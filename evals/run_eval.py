"""Execution-accuracy eval: reflection loop vs. no-repair baseline.

The ablation is *paired*: each question is run once with ``max_repairs=K``. The no-repair
baseline for that question is exactly what the graph would have returned with
``max_repairs=0`` - the first attempt's result if it executed, otherwise a failure - so the
difference between the two columns is the contribution of the reflection step and nothing
else (no sampling noise between two separate runs).

Datasets:
  * bundled ``retail`` benchmark (default): 30 Spider-style questions over evals/bench
  * a Spider subset: ``--spider-dir /path/to/spider --n 100`` (reads dev.json and
    database/<db_id>/<db_id>.sqlite from the official Spider release)

    python evals/run_eval.py --max-repairs 2                 # needs ANTHROPIC_API_KEY
    python evals/run_eval.py --spider-dir ~/data/spider --n 100
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlglot

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "bench"))

from build_db import DEFAULT_PATH, build  # noqa: E402

from text2sql.db import SQLiteDatabase  # noqa: E402
from text2sql.graph import Text2SQLAgent  # noqa: E402
from text2sql.llm import SQLGenerator  # noqa: E402

# --------------------------------------------------------------------------- scoring


def _norm_value(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, bool):
        return int(v)
    return v


def _norm_rows(rows: list[list[Any]]) -> list[tuple]:
    return [tuple(_norm_value(v) for v in row) for row in rows]


def order_matters(gold_sql: str) -> bool:
    try:
        tree = sqlglot.parse_one(gold_sql, read="sqlite")
    except sqlglot.errors.ParseError:
        return "order by" in gold_sql.lower()
    return tree.args.get("order") is not None


def results_match(pred_rows: list[list[Any]], gold_rows: list[list[Any]], ordered: bool) -> bool:
    pred, gold = _norm_rows(pred_rows), _norm_rows(gold_rows)
    if ordered:
        return pred == gold
    return Counter(pred) == Counter(gold)


# --------------------------------------------------------------------------- datasets


def load_bench() -> list[dict]:
    if not DEFAULT_PATH.exists():
        build(DEFAULT_PATH)
    questions = json.loads((HERE / "bench" / "questions.json").read_text())
    return [{**q, "db_path": str(DEFAULT_PATH)} for q in questions]


def load_spider(spider_dir: Path, n: int, seed: int) -> list[dict]:
    dev = json.loads((spider_dir / "dev.json").read_text())
    rng = random.Random(seed)
    sample = rng.sample(dev, min(n, len(dev)))
    return [
        {
            "id": f"spider-{i}",
            "difficulty": ex.get("difficulty", "n/a"),
            "question": ex["question"],
            "query": ex["query"],
            "db_path": str(spider_dir / "database" / ex["db_id"] / f"{ex['db_id']}.sqlite"),
        }
        for i, ex in enumerate(sample)
    ]


# ------------------------------------------------------------------------------ run


def evaluate(examples: list[dict], make_generator, max_repairs: int) -> dict:
    agents: dict[str, Text2SQLAgent] = {}
    generator: SQLGenerator = make_generator()
    records = []
    for ex in examples:
        agent = agents.get(ex["db_path"])
        if agent is None:
            agent = agents[ex["db_path"]] = Text2SQLAgent(
                SQLiteDatabase(ex["db_path"]), generator, max_repairs=max_repairs, max_rows=10_000
            )
        gold = agent.db.execute(ex["query"], max_rows=10_000)
        if not gold.ok:
            records.append({"id": ex["id"], "skipped": f"gold query failed: {gold.error}"})
            continue
        ordered = order_matters(ex["query"])

        cost_before = generator.meter.cost_usd
        started = time.perf_counter()
        try:
            out = agent.ask(ex["question"])
        except Exception as exc:  # scored as wrong, never silently dropped
            out = {
                "status": "failed",
                "attempts": [],
                "result": None,
                "sql": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        latency = time.perf_counter() - started

        final_ok = out["status"] == "succeeded" and results_match(out["result"]["rows"], gold.rows, ordered)
        first_attempt_executed = out["status"] == "succeeded" and not out["attempts"]
        records.append(
            {
                "id": ex["id"],
                "difficulty": ex.get("difficulty", "n/a"),
                "question": ex["question"],
                "gold": ex["query"],
                "pred": out.get("sql"),
                "status": out["status"],
                "attempts": out["attempts"],
                "repairs_used": out.get("repairs_used", 0),
                "first_error_type": out["attempts"][0]["error_type"] if out["attempts"] else None,
                "correct_no_repair": first_attempt_executed and final_ok,
                "correct_with_repair": final_ok,
                "cost_usd": generator.meter.cost_usd - cost_before,
                "latency_s": latency,
                "error": out.get("error"),
            }
        )

    scored = [r for r in records if "skipped" not in r]
    n = len(scored)

    def acc(key: str, rows: list[dict]) -> float:
        return sum(r[key] for r in rows) / len(rows) if rows else 0.0

    by_difficulty = {}
    for level in sorted({r["difficulty"] for r in scored}):
        rows = [r for r in scored if r["difficulty"] == level]
        by_difficulty[level] = {
            "n": len(rows),
            "no_repair": acc("correct_no_repair", rows),
            "with_repair": acc("correct_with_repair", rows),
        }
    needed_repair = [r for r in scored if r["attempts"]]
    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": getattr(generator, "model", type(generator).__name__),
        "max_repairs": max_repairs,
        "n": n,
        "skipped": len(records) - n,
        "execution_accuracy": {
            "no_repair_baseline": acc("correct_no_repair", scored),
            "with_reflection": acc("correct_with_repair", scored),
        },
        "by_difficulty": by_difficulty,
        "first_attempt_failures": len(needed_repair),
        "first_failure_types": dict(Counter(r["first_error_type"] for r in needed_repair)),
        "repaired_to_correct": sum(r["correct_with_repair"] for r in needed_repair),
        "repaired_but_wrong": sum(
            r["status"] == "succeeded" and not r["correct_with_repair"] for r in needed_repair
        ),
        "unrecoverable": sum(r["status"] == "failed" for r in scored),
        "cost_per_question_usd": sum(r["cost_usd"] for r in scored) / n if n else 0.0,
        "mean_latency_s": sum(r["latency_s"] for r in scored) / n if n else 0.0,
        "records": records,
    }


def to_markdown(report: dict) -> str:
    ex = report["execution_accuracy"]
    lines = [
        f"# Text-to-SQL eval - `{report['model']}` (max_repairs={report['max_repairs']}, n={report['n']})",
        "",
        f"_Run {report['run_at']}_",
        "",
        "| System | Execution accuracy |",
        "|---|---|",
        f"| No-repair baseline (first attempt only) | {ex['no_repair_baseline']:.1%} |",
        f"| **With bounded reflection** | **{ex['with_reflection']:.1%}** |",
        "",
        "| Difficulty | n | No repair | With reflection |",
        "|---|---|---|---|",
    ]
    for level, m in report["by_difficulty"].items():
        lines.append(f"| {level} | {m['n']} | {m['no_repair']:.0%} | {m['with_repair']:.0%} |")
    lines += [
        "",
        f"- First attempts that failed to run: {report['first_attempt_failures']} "
        f"({report['first_failure_types']})",
        f"- Repaired to a correct answer: {report['repaired_to_correct']}; repaired but wrong: "
        f"{report['repaired_but_wrong']}; unrecoverable within budget: {report['unrecoverable']}",
        f"- Cost per question: ${report['cost_per_question_usd']:.4f}; mean latency "
        f"{report['mean_latency_s']:.2f}s",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--spider-dir", type=Path)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", type=Path, default=HERE / "results")
    args = parser.parse_args()

    from text2sql.llm import ClaudeSQLGenerator

    examples = load_spider(args.spider_dir, args.n, args.seed) if args.spider_dir else load_bench()
    report = evaluate(examples, lambda: ClaudeSQLGenerator(model=args.model), args.max_repairs)
    args.out.mkdir(exist_ok=True)
    stem = f"{'spider' if args.spider_dir else 'retail'}-{report['model']}-r{args.max_repairs}"
    (args.out / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str))
    (args.out / f"{stem}.md").write_text(to_markdown(report))
    print(to_markdown(report))


if __name__ == "__main__":
    main()
