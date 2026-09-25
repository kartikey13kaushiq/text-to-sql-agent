# Enterprise Text-to-SQL Agent

A cyclic **LangGraph** agent that translates natural-language questions into SQL. It runs the
SQL under a **read-only sandboxed database role** and repairs failed queries through a
**bounded reflection loop**: each retry is told exactly why the last query failed, so it isn't
retrying blindly. An execution-accuracy eval measures how much the reflection step actually
contributes against a no-repair baseline.

```mermaid
flowchart LR
    Q([question]) --> L[load_schema<br/>tables, keys, sample rows]
    L --> G[generate]
    G --> GU{guard<br/>static check}
    GU -- ok --> X{execute<br/>read-only role}
    X -- rows --> OK([succeeded])
    GU -- rejected --> R[reflect]
    X -- error --> R
    R -- "repairs < max" --> GU
    GU -. budget spent .-> F([failed])
    X -. budget spent .-> F
```

## Design

**The sandbox has three independent layers.** A query has to pass all of them:

1. **Static guard** (`guard.py`, sqlglot). The SQL must parse to exactly one statement. It must be
   a query node, with no DML, DDL, `PRAGMA`, `SET`, `ATTACH`, `SELECT ... INTO`, or
   data-modifying CTEs, and every table it references must exist. Rejection reasons are specific
   (`unknown table(s): order_items; available tables: ...`) because they are fed back to the model.
2. **Read-only connection** (`db.py`).
   - SQLite opens with `mode=ro`, plus an authorizer that denies every action except read, select,
     function and recursive CTE.
   - Postgres uses a `default_transaction_read_only` session, rolls back after every statement,
     and connects as the SELECT-only role from `sql/readonly_role.sql`.
   - The tests send writes straight to the connection, **bypassing the guard**, and check that
     they still fail.
3. **Resource limits.** A statement timeout (SQLite progress handler / Postgres
   `statement_timeout`) and a row cap on fetches.

**Bounded reflection.** A failed attempt is appended to the state as
`{sql, stage, error_type, error}`, and the repair prompt receives **all** earlier failures.
Error types are `guard | syntax | unknown_identifier | type | timeout | permission | runtime`.
`max_repairs` caps the loop, and `max_repairs=0` is the no-repair baseline.

**Schema context.** The model sees `CREATE TABLE`-style definitions with primary and foreign
keys, plus sample rows, so literal values match the stored format. Examples are upper-case
status codes and prices in integer cents. The schema goes in a `cache_control` system block,
because it's identical across questions on one database. Postgres introspection uses
`pg_catalog`, not `information_schema`, because the latter hides constraints from a role
that owns no tables. The Postgres integration test caught that bug.

**Structured outputs.** Generation and repair use `client.messages.parse` with Pydantic models
(`SQLCandidate{reasoning, sql}` and `SQLRepair{diagnosis, sql}`).

## Evaluation

`evals/run_eval.py` computes **execution accuracy**: predicted and gold result sets are compared
as multisets, in order only when the gold query has a top-level `ORDER BY`, with floats rounded.
The **repair vs no-repair ablation is paired**. Each question runs once with `max_repairs=K`.
Its no-repair score is exactly what `max_repairs=0` would have returned: the first attempt if
it executed, otherwise a failure. The difference between the two columns is therefore the
reflection step and nothing else. The harness also reports first-attempt error types,
repaired-to-correct, repaired-but-wrong, cost per question and latency.

Two datasets:

- **Spider subset** (the resume's benchmark). Download the official Spider release and run
  `python evals/run_eval.py --spider-dir path/to/spider --n 100 --max-repairs 2`.
  The harness reads `dev.json` and `database/<db_id>/<db_id>.sqlite`, and samples with a fixed seed.
- **Bundled `retail` benchmark.** 30 Spider-style questions (easy to extra) over a deterministic
  7-table database built by `evals/bench/build_db.py`. It's designed with names a model can't
  guess (`order_line`, `unit_price_cents`, a self-join on `manager_id`), so the repair loop gets
  exercised. Run it with `python evals/run_eval.py --max-repairs 2`.

Both need `ANTHROPIC_API_KEY`. No model results are committed: this repository was built
without API access, and scores should come from a real run, not be written in by hand. Reports
land in `evals/results/`. The harness logic itself is covered by `tests/test_eval.py`, using a
scripted "noisy oracle" whose expected baseline and repair scores are known exactly.

## Run it

```bash
pip install -e ".[dev,postgres,api]"
python evals/bench/build_db.py                       # evals/bench/retail.sqlite
export ANTHROPIC_API_KEY=...
text2sql ask "Which 5 products generated the most shipped revenue?" --db evals/bench/retail.sqlite
text2sql ask "..." --db postgresql://text2sql_ro:...@localhost:5432/analytics
text2sql ask "" --db evals/bench/retail.sqlite --schema     # what the model sees

TEXT2SQL_DATABASE=evals/bench/retail.sqlite uvicorn text2sql.api:app
curl -s localhost:8000/query -H 'content-type: application/json' -d '{"question": "How many gold tier customers are there?"}'
```

Or from the repo root: `docker compose up --build`. That brings up the API on :8002, connected
to Postgres as the SELECT-only role.

Configuration: `TEXT2SQL_DATABASE`, `TEXT2SQL_MODEL` (default `claude-opus-5`), `TEXT2SQL_MAX_REPAIRS`.

## Tests

```bash
pytest -q                                                         # 35 tests, offline
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/t2s pytest tests/test_postgres.py
```

The Postgres tests load the benchmark, create the SELECT-only role, and check three things:
writes fail with a permission error even without the guard, the timeout fires, and the agent
repairs a query end to end against Postgres.

## Layout

```
src/text2sql/
  graph.py    load_schema -> generate -> guard -> execute, with the reflect cycle
  guard.py    sqlglot static checks
  db.py       SQLite / Postgres read-only sandboxes, schema description, error typing
  llm.py      Claude generator (structured outputs, prompt caching, cost metering), scripted generator
  api.py cli.py
sql/          SELECT-only role, Postgres loader for the benchmark
evals/        run_eval.py (execution accuracy, paired ablation), bench/ (retail db + questions)
```
