"""Cyclic Text-to-SQL graph with a bounded reflection loop.

    START -> load_schema -> generate -> guard --ok--> execute --ok--> done -> END
                                          |                  |
                                        error              error
                                          v                  v
                                         reflect <-----------+   (while repairs < max_repairs)
                                          |
                                          +--> guard ...          (else -> failed -> END)

The reflection step sees the full list of failed attempts with their error type and message,
so each repair is informed by *why* the previous query failed rather than retrying blindly.
``max_repairs=0`` turns the loop off, which is the no-repair baseline used in the eval.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from .db import Database
from .guard import check_sql
from .llm import SQLGenerator


class Text2SQLState(TypedDict, total=False):
    question: str
    schema: str
    sql: str
    attempts: Annotated[list[dict[str, Any]], operator.add]
    result: dict[str, Any]
    status: Literal["running", "succeeded", "failed"]


def build_graph(db: Database, generator: SQLGenerator, max_repairs: int = 2, max_rows: int = 200):
    schema_cache: dict[str, Any] = {}

    def _schema() -> tuple[str, set[str]]:
        if not schema_cache:
            schema_cache["text"] = db.describe_schema()
            schema_cache["tables"] = set(db.table_names())
        return schema_cache["text"], schema_cache["tables"]

    def load_schema(state: Text2SQLState) -> dict:
        text, _ = _schema()
        return {"schema": text, "status": "running"}

    def generate(state: Text2SQLState) -> dict:
        candidate = generator.generate(state["question"], state["schema"], db.dialect)
        return {"sql": candidate.sql.strip().rstrip(";")}

    def _fail_or_reflect(state: Text2SQLState, attempt: dict) -> Command:
        repairs_used = len(state.get("attempts", []))  # attempts so far, excluding this one
        if repairs_used < max_repairs:
            return Command(goto="reflect", update={"attempts": [attempt]})
        return Command(
            goto=END,
            update={"attempts": [attempt], "status": "failed", "result": {"ok": False, **attempt}},
        )

    def guard(state: Text2SQLState) -> Command[Literal["execute", "reflect", "__end__"]]:
        _, tables = _schema()
        verdict = check_sql(state["sql"], db.dialect, tables)
        if verdict.ok:
            return Command(goto="execute")
        return _fail_or_reflect(
            state, {"sql": state["sql"], "stage": "guard", "error_type": "guard", "error": verdict.reason}
        )

    def execute(state: Text2SQLState) -> Command[Literal["reflect", "__end__"]]:
        result = db.execute(state["sql"], max_rows=max_rows)
        if result.ok:
            return Command(goto=END, update={"result": result.model_dump(), "status": "succeeded"})
        return _fail_or_reflect(
            state,
            {"sql": state["sql"], "stage": "execute", "error_type": result.error_type, "error": result.error},
        )

    def reflect(state: Text2SQLState) -> dict:
        repair = generator.repair(state["question"], state["schema"], db.dialect, state["attempts"])
        return {"sql": repair.sql.strip().rstrip(";")}

    graph = StateGraph(Text2SQLState)
    graph.add_node("load_schema", load_schema)
    graph.add_node("generate", generate)
    graph.add_node("guard", guard)
    graph.add_node("execute", execute)
    graph.add_node("reflect", reflect)
    graph.add_edge(START, "load_schema")
    graph.add_edge("load_schema", "generate")
    graph.add_edge("generate", "guard")
    graph.add_edge("reflect", "guard")
    return graph.compile()


class Text2SQLAgent:
    def __init__(self, db: Database, generator: SQLGenerator, max_repairs: int = 2, max_rows: int = 200):
        self.db = db
        self.generator = generator
        self.max_repairs = max_repairs
        self.graph = build_graph(db, generator, max_repairs=max_repairs, max_rows=max_rows)

    def ask(self, question: str) -> dict[str, Any]:
        state = self.graph.invoke(
            {"question": question, "attempts": []},
            {"recursion_limit": 6 + 3 * self.max_repairs},
        )
        return {
            "question": question,
            "status": state["status"],
            "sql": state.get("sql"),
            "result": state.get("result"),
            "attempts": state.get("attempts", []),
            "repairs_used": len(state.get("attempts", []))
            if state["status"] == "succeeded"
            else max(0, len(state.get("attempts", [])) - 1),
        }
