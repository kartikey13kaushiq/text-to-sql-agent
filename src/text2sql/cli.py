"""``text2sql ask "question" --db retail.sqlite``"""

from __future__ import annotations

import argparse
import json
import os

from .db import connect
from .graph import Text2SQLAgent
from .llm import ClaudeSQLGenerator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="text2sql")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument(
        "--db", default=os.environ.get("TEXT2SQL_DATABASE"), help="SQLite path or postgresql:// URL"
    )
    ask.add_argument("--max-repairs", type=int, default=2)
    ask.add_argument("--schema", action="store_true", help="print the schema the model sees and exit")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("--db or TEXT2SQL_DATABASE is required")

    db = connect(args.db)
    if args.schema:
        print(db.describe_schema())
        return 0
    out = Text2SQLAgent(db, ClaudeSQLGenerator(), max_repairs=args.max_repairs).ask(args.question)
    for i, attempt in enumerate(out["attempts"], start=1):
        print(f"-- attempt {i} failed ({attempt['error_type']}): {attempt['error']}\n{attempt['sql']}\n")
    print(f"-- final ({out['status']}, repairs used: {out['repairs_used']})\n{out['sql']}\n")
    if out["status"] == "succeeded":
        result = out["result"]
        print(json.dumps({"columns": result["columns"], "rows": result["rows"][:50]}, indent=2, default=str))
    return 0 if out["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
