from text2sql.graph import Text2SQLAgent
from text2sql.llm import ScriptedGenerator

Q = "How many order lines are there?"


def agent(db, sqls, max_repairs=2):
    gen = ScriptedGenerator({Q: sqls})
    return Text2SQLAgent(db, gen, max_repairs=max_repairs), gen


def test_first_attempt_success(db):
    a, gen = agent(db, ["SELECT COUNT(*) FROM order_line"])
    out = a.ask(Q)
    assert out["status"] == "succeeded"
    assert out["attempts"] == [] and out["repairs_used"] == 0
    assert gen.meter.calls == 1


def test_guard_failure_is_repaired(db):
    a, _ = agent(db, ["SELECT COUNT(*) FROM order_items", "SELECT COUNT(*) FROM order_line"])
    out = a.ask(Q)
    assert out["status"] == "succeeded"
    assert out["repairs_used"] == 1
    assert out["attempts"][0]["stage"] == "guard"
    assert "order_items" in out["attempts"][0]["error"]


def test_execution_failure_is_repaired_with_history(db):
    a, _ = agent(
        db,
        [
            "SELECT COUNT(*) FROM order_items",  # guard: unknown table
            "SELECT COUNT(line_id) FROM order_line",  # sqlite: no such column
            "SELECT COUNT(*) FROM order_line",
        ],
    )
    out = a.ask(Q)
    assert out["status"] == "succeeded"
    assert [x["stage"] for x in out["attempts"]] == ["guard", "execute"]
    assert out["attempts"][1]["error_type"] == "unknown_identifier"
    assert out["repairs_used"] == 2


def test_repair_budget_is_enforced(db):
    a, gen = agent(db, ["SELECT x FROM nowhere"], max_repairs=2)
    out = a.ask(Q)
    assert out["status"] == "failed"
    assert len(out["attempts"]) == 3  # initial + 2 repairs
    assert out["repairs_used"] == 2
    assert gen.meter.calls == 3


def test_no_repair_baseline(db):
    a, gen = agent(db, ["SELECT COUNT(*) FROM order_items", "SELECT COUNT(*) FROM order_line"], max_repairs=0)
    out = a.ask(Q)
    assert out["status"] == "failed"
    assert gen.meter.calls == 1


def test_write_attempt_never_reaches_the_database(db):
    a, _ = agent(db, ["DELETE FROM order_line", "SELECT COUNT(*) FROM order_line"])
    out = a.ask(Q)
    assert out["attempts"][0]["stage"] == "guard"
    assert out["result"]["rows"][0][0] > 0
