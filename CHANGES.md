# Changes: Extending AutoMaintainer into a Standalone Code Reviewer

This documents everything added on top of the base AutoMaintainer codebase
for Google Code Kitchen — Problem Statement 1 ("The 24/7 Intelligent Code
Reviewer"). See `AutoMaintainer_Extension_Report.md` for the full design
rationale; this file is the concrete "what actually changed" record.

## New files

| File | Purpose |
|---|---|
| `backend/rules_engine.py` | CSV ingestion + TF-IDF retrieval for historical review rules |
| `backend/static_analysis.py` | Ruff-based static analysis for Python (interface extensible to other languages) |
| `backend/scoring.py` | Weighted 1–10 rubric scoring engine, with hard caps for critical findings |
| `backend/review_engine.py` | Orchestrates AST parsing + static analysis + rule grounding + LLM review into one result |
| `backend/test_ast_multilang.py` | Tests for Java/C/C++/Go/Rust AST extraction |
| `backend/test_rules_engine.py` | Tests for CSV parsing and rule retrieval, incl. a regression test for a real grounding gap found during development |
| `backend/test_scoring.py` | Tests for the rubric engine, incl. a regression test for a real scoring-calibration bug found during development |
| `backend/test_review_engine.py` | Tests for LLM-response parsing and the full orchestration path (stubbed LLM call) |
| `supabase_schema_review_extension.sql` | `historical_rules` and `review_sessions` tables, RLS policies, `usage_events` CHECK constraint update |
| `dashboard/src/lib/hooks/use-review.ts` | Frontend hook: submit, poll, history, rule ingestion |
| `dashboard/src/components/dashboard/ReviewView.tsx` | Submission form, results panel, score-trend chart, history list |

## Modified files

| File | What changed |
|---|---|
| `backend/ast_indexer.py` | Rewrote to support Java, C, C++, Go, Rust in addition to Python/JS/TS. Refactored to a declarative per-language rule table instead of a hard-coded if/elif chain. Added `parse_source()` for in-memory code (no disk write needed for ad-hoc submissions). |
| `backend/tasks.py` | Added `run_code_review_task` (Celery) and `update_review_session_status()`, mirroring the existing `run_agent_loop_task` pattern but targeting `review_sessions` with a 120s soft time limit instead of 30 minutes. |
| `backend/main.py` | Added `POST /review/submit`, `GET /review/status/{id}`, `GET /review/history`, `POST /review/rules/ingest`. |
| `backend/requirements.in` / `requirements.txt` | Added tree-sitter grammars (java/c/cpp/go/rust), scikit-learn, ruff, and their transitive deps. |
| `dashboard/src/components/dashboard/Sidebar.tsx` | Added "Code Review" nav item. |
| `dashboard/src/components/dashboard/DashboardShell.tsx` | Wired the new `review` tab into the view switcher. |
| `dashboard/package.json` | Added `recharts` for the score-trend chart. |

## What's tested vs. not

Everything above the LLM call itself has been run against real inputs during
development: all 8 languages' AST extraction, CSV parsing against the exact
schema from the problem statement, the rubric scorer across clean/vulnerable/
worst-case code, and the full review orchestration with a stubbed LLM
response. `main.py` and `tasks.py` were verified by actually importing the
full module chain (not just a syntax check) — routes went from 24 to 28 with
no import errors. The frontend was type-checked (`tsc --noEmit`), linted
(`eslint`), and pushed through `next build` up to the point where Turbopack
tries to fetch Google Fonts (blocked only by this sandbox's network
allowlist — confirmed identical on an unmodified copy of the base project).

**Not tested here:** an actual live call to the Groq API — `api.groq.com`
isn't reachable from the sandbox this was built in. Before you demo, submit
a real review through `/review/submit` end-to-end once your `GROQ_API_KEY`
and Supabase credentials are live, to confirm the model's actual output
matches what `parse_llm_review_response()` expects.

## Before you run this for real

1. Apply `supabase_schema_review_extension.sql` after the base
   `supabase_schema.sql`.
2. Point Celery at the new `code_reviews` queue (or let it consume from the
   default queue — check your worker's `-Q` flag / `celery_app.py` routing
   config).
3. Run `npm run dev` locally (or `npm run build` anywhere with normal
   internet access) to sanity-check the dashboard — this sandbox couldn't
   complete a full build due to font-fetching being blocked, not because of
   anything in the new code.
4. Two real bugs were found and fixed during development, not left as
   surprises: a scoring bug where a SQL-injection finding only dragged the
   overall score to 8.6/10 (fixed with a hard cap for critical findings —
   see `scoring.py`), and a schema bug where `org_id` was typed `BIGINT`
   instead of `UUID` (fixed before it ever hit a real migration).
