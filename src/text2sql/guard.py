"""Static guard for generated SQL (first layer of the sandbox).

A statement passes only if it parses, is exactly one statement, is a read-only query,
and references only tables that exist. Rejections carry a precise reason, which is fed
back to the model in the repair loop.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

_WRITE_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,  # anything sqlglot cannot model (GRANT, COPY, VACUUM, ...)
    exp.Pragma,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)

_DIALECTS = {"sqlite": "sqlite", "postgres": "postgres"}


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reason: str | None = None


def check_sql(sql: str, dialect: str, known_tables: set[str]) -> GuardResult:
    try:
        statements = [s for s in sqlglot.parse(sql, read=_DIALECTS.get(dialect, dialect)) if s is not None]
    except ParseError as exc:
        return GuardResult(False, f"could not parse SQL: {str(exc).splitlines()[0]}")
    if len(statements) != 1:
        return GuardResult(False, f"expected exactly one statement, got {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        return GuardResult(False, f"only read-only queries are allowed, got {tree.key.upper()}")
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            return GuardResult(False, f"write or DDL operation {node.key.upper()} is not allowed")
        if isinstance(node, exp.Into):
            return GuardResult(False, "SELECT ... INTO is not allowed")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    known = {t.lower() for t in known_tables}
    unknown = sorted(
        {
            t.name
            for t in tree.find_all(exp.Table)
            if t.name and t.name.lower() not in known and t.name.lower() not in cte_names
        }
    )
    if unknown:
        return GuardResult(
            False,
            f"unknown table(s): {', '.join(unknown)}; available tables: {', '.join(sorted(known_tables))}",
        )
    return GuardResult(True)
