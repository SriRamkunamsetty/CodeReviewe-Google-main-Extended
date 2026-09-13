"""
Coverage for the multi-language extension of ast_indexer.py
(improvement #4.2 groundwork / the "multi-language reviews" requirement
of Problem Statement 1). test_ast.py already covers Python thoroughly;
this file covers the newly-added languages plus the in-memory
parse_source() path the review engine uses for ad-hoc submissions
(submitted code never touches disk).
"""

from ast_indexer import TreeSitterParser, detect_extension


def test_supported_extensions_include_all_new_languages():
    parser = TreeSitterParser()
    supported = parser.supported_extensions()
    for ext in [".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs"]:
        assert ext in supported


def test_java_class_and_method():
    code = "public class Foo {\n  public int bar(int x) { return x + 1; }\n}\n"
    result = TreeSitterParser().parse_source(code, ".java")
    assert result["classes"][0]["name"] == "Foo"
    assert any(f["name"] == "bar" for f in result["functions"])


def test_c_struct_and_function_name_via_nested_declarator():
    code = "struct Point { int x; int y; };\nint add(int a, int b) {\n  return a+b;\n}\n"
    result = TreeSitterParser().parse_source(code, ".c")
    assert result["classes"][0]["name"] == "Point"
    assert any(f["name"] == "add" for f in result["functions"])


def test_cpp_class_and_method():
    code = "class Shape {\n public:\n  int area() { return 0; }\n};\n"
    result = TreeSitterParser().parse_source(code, ".cpp")
    assert result["classes"][0]["name"] == "Shape"
    assert any(f["name"] == "area" for f in result["functions"])


def test_go_struct_function_and_method():
    code = (
        "package main\n"
        "func add(a int, b int) int { return a+b }\n"
        "type Point struct { X int\n Y int }\n"
        "func (p Point) Sum() int { return p.X+p.Y }\n"
    )
    result = TreeSitterParser().parse_source(code, ".go")
    assert any(c["name"] == "Point" for c in result["classes"])
    names = [f["name"] for f in result["functions"]]
    assert "add" in names and "Sum" in names


def test_rust_struct_impl_and_function():
    code = (
        "struct Point { x: i32, y: i32 }\n"
        "impl Point {\n  fn sum(&self) -> i32 { self.x + self.y }\n}\n"
        "fn add(a: i32, b: i32) -> i32 { a + b }\n"
    )
    result = TreeSitterParser().parse_source(code, ".rs")
    class_names = [c["name"] for c in result["classes"]]
    assert "Point" in class_names  # both struct_item and impl_item resolve to it
    func_names = [f["name"] for f in result["functions"]]
    assert "sum" in func_names and "add" in func_names


def test_unsupported_extension_returns_none():
    result = TreeSitterParser().parse_source("print 'hi'", ".cobol")
    assert result is None


def test_detect_extension_from_language_hint():
    assert detect_extension("submission.txt", language_hint="Python") == ".py"
    assert detect_extension("submission.txt", language_hint="go") == ".go"
    assert detect_extension("Main.java", language_hint=None) == ".java"
