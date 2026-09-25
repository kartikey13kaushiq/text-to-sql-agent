"""Harness mechanics with a scripted "noisy oracle": no model involved."""

import json
from pathlib import Path

from run_eval import evaluate, order_matters, results_match

from text2sql.llm import ScriptedGenerator

BENCH = Path(__file__).parent.parent / "evals" / "bench" / "questions.json"


def test_result_matching_rules():
    assert results_match([[1], [2]], [[2], [1]], ordered=False)
    assert not results_match([[1], [2]], [[2], [1]], ordered=True)
    assert results_match([[0.333333333]], [[0.33333]], ordered=True)
    assert not results_match([[1], [1]], [[1]], ordered=False)  # multiset, not set
    assert order_matters("SELECT a FROM t ORDER BY a")
    assert not order_matters("SELECT a FROM (SELECT a FROM t ORDER BY a) s")


def test_paired_ablation_isolates_the_repair_step(retail_path):
    questions = json.loads(BENCH.read_text())
    examples = [{**q, "db_path": str(retail_path)} for q in questions]
    script = {}
    for i, q in enumerate(questions):
        if i % 3 == 0:  # first attempt references a table that does not exist, repair returns gold
            script[q["question"]] = [q["query"].replace("FROM ", "FROM tbl_", 1), q["query"]]
        elif i % 3 == 1:  # first attempt runs but is wrong: repair never triggers
            script[q["question"]] = ["SELECT 1"]
        else:
            script[q["question"]] = [q["query"]]

    report = evaluate(examples, lambda: ScriptedGenerator(script), max_repairs=2)
    assert report["n"] == 30 and report["skipped"] == 0
    assert report["execution_accuracy"]["no_repair_baseline"] == 10 / 30
    assert report["execution_accuracy"]["with_reflection"] == 20 / 30
    assert report["repaired_to_correct"] == 10
    assert sum(report["first_failure_types"].values()) == 10  # caught by the guard or by the database
