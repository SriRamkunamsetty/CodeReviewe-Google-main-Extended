import os
import json
from pathlib import Path
from typing import Callable, Optional

from tree_sitter import Language, Node, Parser

import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_typescript

# --- New language grammars added for Problem-1 "multi-language reviews" ---
# Each of these is a small, independent PyPI package (pure compiled grammar,
# no system compiler toolchain required at install time).
import tree_sitter_java
import tree_sitter_c
import tree_sitter_cpp
import tree_sitter_go
import tree_sitter_rust

# Initialize Languages (existing)
PY_LANGUAGE = Language(tree_sitter_python.language())
JS_LANGUAGE = Language(tree_sitter_javascript.language())
TS_LANGUAGE = Language(tree_sitter_typescript.language_typescript())

# Initialize Languages (new)
JAVA_LANGUAGE = Language(tree_sitter_java.language())
C_LANGUAGE = Language(tree_sitter_c.language())
CPP_LANGUAGE = Language(tree_sitter_cpp.language())
GO_LANGUAGE = Language(tree_sitter_go.language())
RUST_LANGUAGE = Language(tree_sitter_rust.language())


def _resolve_declarator_name(node: Node) -> Optional[Node]:
    """
    C/C++ wrap the identifier inside nested declarators, e.g. a
    function_definition's name lives at declarator.declarator (and deeper
    still for pointer/array declarators: ``int *add(...)`` etc).
    Walk down the 'declarator' field chain until we hit a plain identifier.
    """
    current: Optional[Node] = node
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.type in ("identifier", "field_identifier", "type_identifier"):
            return current
        inner = current.child_by_field_name("declarator")
        if inner is not None:
            current = inner
            continue
        for child in current.children:
            if child.type in ("identifier", "field_identifier"):
                return child
        break
    return None


def _name_via_field(node: Node, code: str) -> Optional[str]:
    name_node = node.child_by_field_name("name")
    if name_node:
        return code[name_node.start_byte : name_node.end_byte]
    return None


def _name_via_declarator(node: Node, code: str) -> Optional[str]:
    declarator = node.child_by_field_name("declarator")
    if not declarator:
        return None
    resolved = _resolve_declarator_name(declarator)
    if resolved:
        return code[resolved.start_byte : resolved.end_byte]
    return None


def _name_via_type_field(node: Node, code: str) -> Optional[str]:
    """Rust `impl_item` has no 'name' field; the type being implemented
    lives in the 'type' field instead."""
    type_node = node.child_by_field_name("type")
    if type_node:
        return code[type_node.start_byte : type_node.end_byte]
    return None


# Declarative per-language extraction rules. Each entry maps a tree-sitter
# node type to the bucket it belongs in ("classes" / "functions" / "imports")
# plus the resolver function used to pull out its display name.
# This replaces the old single hard-coded if/elif walk so a new language is
# a data addition, not a new branch of custom logic.
LANGUAGE_RULES: dict[str, dict] = {
    ".py": {
        "language": PY_LANGUAGE,
        "classes": [("class_definition", _name_via_field)],
        "functions": [("function_definition", _name_via_field)],
        "imports": ["import_statement", "import_from_statement"],
    },
    ".js": {
        "language": JS_LANGUAGE,
        "classes": [("class_declaration", _name_via_field)],
        "functions": [
            ("function_declaration", _name_via_field),
            ("method_definition", _name_via_field),
            ("arrow_function", _name_via_field),
        ],
        "imports": ["import_statement"],
    },
    ".ts": {
        "language": TS_LANGUAGE,
        "classes": [("class_declaration", _name_via_field)],
        "functions": [
            ("function_declaration", _name_via_field),
            ("method_definition", _name_via_field),
            ("arrow_function", _name_via_field),
        ],
        "imports": ["import_statement"],
    },
    ".java": {
        "language": JAVA_LANGUAGE,
        "classes": [
            ("class_declaration", _name_via_field),
            ("interface_declaration", _name_via_field),
        ],
        "functions": [("method_declaration", _name_via_field)],
        "imports": ["import_declaration"],
    },
    ".c": {
        "language": C_LANGUAGE,
        "classes": [("struct_specifier", _name_via_field)],
        "functions": [("function_definition", _name_via_declarator)],
        "imports": ["preproc_include"],
    },
    ".cpp": {
        "language": CPP_LANGUAGE,
        "classes": [
            ("class_specifier", _name_via_field),
            ("struct_specifier", _name_via_field),
        ],
        "functions": [("function_definition", _name_via_declarator)],
        "imports": ["preproc_include"],
    },
    ".go": {
        "language": GO_LANGUAGE,
        "classes": [("type_spec", _name_via_field)],
        "functions": [
            ("function_declaration", _name_via_field),
            ("method_declaration", _name_via_field),
        ],
        "imports": ["import_spec"],
    },
    ".rs": {
        "language": RUST_LANGUAGE,
        "classes": [
            ("struct_item", _name_via_field),
            ("enum_item", _name_via_field),
            ("impl_item", _name_via_type_field),
        ],
        "functions": [("function_item", _name_via_field)],
        "imports": ["use_declaration"],
    },
}

# Extra extensions that map onto an already-defined ruleset
LANGUAGE_RULES[".jsx"] = LANGUAGE_RULES[".js"]
LANGUAGE_RULES[".mjs"] = LANGUAGE_RULES[".js"]
LANGUAGE_RULES[".tsx"] = LANGUAGE_RULES[".ts"]
LANGUAGE_RULES[".h"] = LANGUAGE_RULES[".c"]
LANGUAGE_RULES[".hpp"] = LANGUAGE_RULES[".cpp"]
LANGUAGE_RULES[".cc"] = LANGUAGE_RULES[".cpp"]
LANGUAGE_RULES[".cxx"] = LANGUAGE_RULES[".cpp"]

# Human-readable language name, used for review prompts / UI labels.
EXTENSION_TO_LANGUAGE_NAME: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript (JSX)",
    ".mjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (TSX)",
    ".java": "Java",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".go": "Go",
    ".rs": "Rust",
}


class TreeSitterParser:
    def __init__(self):
        self.parser = Parser()
        # Preserved for backward compatibility with any external code that
        # inspected `.language_map` directly.
        self.language_map = {ext: rules["language"] for ext, rules in LANGUAGE_RULES.items()}

    def supported_extensions(self) -> list[str]:
        return sorted(LANGUAGE_RULES.keys())

    def language_name_for(self, filepath: str) -> str:
        ext = Path(filepath).suffix
        return EXTENSION_TO_LANGUAGE_NAME.get(ext, ext.lstrip(".") or "unknown")

    def parse_file(self, filepath: str):
        ext = Path(filepath).suffix
        return self.parse_source(None, ext, filepath=filepath)

    def parse_source(
        self, source: Optional[str], ext: str, filepath: str = "<submitted>"
    ):
        """
        Parse either a file on disk (source=None, reads filepath) or an
        in-memory code string (source=<code>, ext tells us which grammar).
        The in-memory path is what the new ad-hoc review endpoint uses —
        submitted code never has to touch disk to be structurally analyzed.
        """
        rules = LANGUAGE_RULES.get(ext)
        if rules is None:
            return None

        if source is None:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    code = f.read()
            except Exception:
                return None
        else:
            code = source

        self.parser.language = rules["language"]
        tree = self.parser.parse(bytes(code, "utf8"))

        classes = []
        functions = []
        imports = []

        class_node_types = {t: resolver for t, resolver in rules["classes"]}
        function_node_types = {t: resolver for t, resolver in rules["functions"]}
        import_node_types = set(rules["imports"])

        def walk(node: Node):
            node_type = node.type

            if node_type in class_node_types:
                name = class_node_types[node_type](node, code)
                if name:
                    classes.append(
                        {
                            "name": name,
                            "start_line": node.start_point[0] + 1,
                            "end_line": node.end_point[0] + 1,
                        }
                    )
            elif node_type in function_node_types:
                name = function_node_types[node_type](node, code) or "<anonymous>"
                functions.append(
                    {
                        "name": name,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    }
                )
            elif node_type in import_node_types:
                imports.append(
                    {
                        "statement": code[node.start_byte : node.end_byte],
                        "line": node.start_point[0] + 1,
                    }
                )

            for child in node.children:
                walk(child)

        walk(tree.root_node)

        return {"classes": classes, "functions": functions, "imports": imports}


class CodebaseMapper:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self.parser = TreeSitterParser()
        self.ignored_dirs = {
            ".git",
            "node_modules",
            "__pycache__",
            "venv",
            "env",
            "build",
            "dist",
            "target",  # Rust/Go build output
        }

    def generate_architecture_map(self):
        architecture_map = {}

        for root, dirs, files in os.walk(self.repo_path):
            # Prune ignored directories
            dirs[:] = [d for d in dirs if d not in self.ignored_dirs]

            for file in files:
                filepath = Path(root) / file
                ext = filepath.suffix

                # Only parse supported files to save time
                if ext in self.parser.language_map:
                    rel_path = filepath.relative_to(self.repo_path).as_posix()
                    parsed_data = self.parser.parse_file(str(filepath))
                    if parsed_data and (
                        parsed_data["classes"] or parsed_data["functions"]
                    ):
                        architecture_map[rel_path] = parsed_data

        return architecture_map

    def export_map(self, output_file="architecture_map.json"):
        arch_map = self.generate_architecture_map()
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(arch_map, f, indent=2)
        return arch_map


def detect_extension(filename: str, language_hint: Optional[str] = None) -> str:
    """
    Resolve a file extension for AST parsing given a filename and/or an
    explicit language hint from the submitter (e.g. dashboard dropdown).
    Falls back to '.py' style guessing only as a last resort.
    """
    if language_hint:
        hint = language_hint.strip().lower()
        alias_map = {
            "python": ".py",
            "py": ".py",
            "javascript": ".js",
            "js": ".js",
            "typescript": ".ts",
            "ts": ".ts",
            "java": ".java",
            "c": ".c",
            "c++": ".cpp",
            "cpp": ".cpp",
            "go": ".go",
            "golang": ".go",
            "rust": ".rs",
            "rs": ".rs",
        }
        if hint in alias_map:
            return alias_map[hint]

    ext = Path(filename).suffix
    if ext in LANGUAGE_RULES:
        return ext
    return ext  # unknown extensions are returned as-is; caller checks support


if __name__ == "__main__":
    # Simple test when run locally
    mapper = CodebaseMapper(".")
    arch = mapper.generate_architecture_map()
    print(json.dumps(arch, indent=2))
