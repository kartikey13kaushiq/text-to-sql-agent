"""FastAPI surface: POST /query {"question": "..."}.

Configure with TEXT2SQL_DATABASE (a SQLite path or a postgresql:// URL for the read-only role).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from .db import connect
from .graph import Text2SQLAgent
from .llm import make_generator

app = FastAPI(title="Text-to-SQL Agent", version="0.1.0")


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


@lru_cache(maxsize=1)
def get_agent() -> Text2SQLAgent:
    return Text2SQLAgent(
        connect(os.environ["TEXT2SQL_DATABASE"]),
        make_generator(),
        max_repairs=int(os.environ.get("TEXT2SQL_MAX_REPAIRS", "2")),
    )


Agent = Annotated[Text2SQLAgent, Depends(get_agent)]


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/query")
def query(body: QueryRequest, agent: Agent) -> dict:
    return agent.ask(body.question)
