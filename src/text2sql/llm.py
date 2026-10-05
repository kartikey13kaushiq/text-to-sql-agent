"""SQL generation and reflection backends."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)

PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class SQLCandidate(BaseModel):
    reasoning: str = Field(description="Brief plan: which tables, joins, filters and aggregates answer it.")
    sql: str = Field(description="A single read-only SQL query in the target dialect, no trailing semicolon.")


class SQLRepair(BaseModel):
    diagnosis: str = Field(description="Why the previous query failed, in one or two sentences.")
    sql: str = Field(description="The corrected single read-only SQL query.")


@dataclass
class UsageMeter:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0

    def record(self, model: str, input_tokens: int, output_tokens: int, latency_s: float) -> None:
        price_in, price_out = PRICING.get(model, (0.0, 0.0))
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += (input_tokens * price_in + output_tokens * price_out) / 1_000_000
        self.latency_s += latency_s


class SQLGenerator(Protocol):
    meter: UsageMeter

    def generate(self, question: str, schema: str, dialect: str) -> SQLCandidate: ...

    def repair(self, question: str, schema: str, dialect: str, attempts: list[dict[str, Any]]) -> SQLRepair: ...


SYSTEM_PROMPT = """You translate business questions into SQL over the database schema you are given.

Rules:
- Write exactly one read-only query (SELECT or WITH ... SELECT) in the requested dialect.
- Use only tables and columns that appear in the schema. Match literal values to the formats \
shown in the sample rows (case, date format, codes).
- Return only the columns the question asks for, in the order it asks for them. Do not add \
ORDER BY or LIMIT unless the question implies an ordering or a top-N.
- Your query runs under a read-only role with a statement timeout; any write is rejected."""


class ClaudeSQLGenerator:
    def __init__(self, model: str | None = None, client: Any | None = None):
        import anthropic

        self.model = model or os.environ.get("TEXT2SQL_MODEL", "claude-opus-5")
        self.client = client or anthropic.Anthropic()
        self.meter = UsageMeter()

    def _parse(self, prompt: str, schema_block: str, output: type[T]) -> T:
        started = time.perf_counter()
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            # The schema is identical across questions on the same database: cache it.
            system=[
                {"type": "text", "text": SYSTEM_PROMPT},
                {"type": "text", "text": schema_block, "cache_control": {"type": "ephemeral"}},
            ],
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
            output_format=output,
        )
        self.meter.record(
            self.model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            time.perf_counter() - started,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError(f"model returned no structured output (stop_reason={response.stop_reason})")
        return response.parsed_output

    @staticmethod
    def _schema_block(schema: str, dialect: str) -> str:
        return f"<dialect>{dialect}</dialect>\n<schema>\n{schema}\n</schema>"

    def generate(self, question: str, schema: str, dialect: str) -> SQLCandidate:
        return self._parse(f"<question>{question}</question>", self._schema_block(schema, dialect), SQLCandidate)

    def repair(self, question, schema, dialect, attempts) -> SQLRepair:
        history = json.dumps(attempts, indent=2, default=str)
        prompt = (
            f"<question>{question}</question>\n\n<failed_attempts>\n{history}\n</failed_attempts>\n\n"
            "Every attempt above failed with the error shown. Diagnose the most recent failure against "
            "the schema, then write a corrected query. Do not repeat a query that already failed."
        )
        return self._parse(prompt, self._schema_block(schema, dialect), SQLRepair)


class LLMSQLGenerator:
    """Provider-agnostic generator over any ``agentkit`` model (OpenAI-compatible, Anthropic, Gemini, local)."""

    def __init__(self, model: Any):
        self.chat_model = model
        self.model = f"{model.provider}:{model.model}"
        self.meter = UsageMeter()

    def _parse(self, prompt: str, schema_block: str, output: type[T]) -> T:
        from agentkit import generate_structured
        from agentkit.pricing import cost_usd

        result = generate_structured(
            self.chat_model, output, prompt, system=f"{SYSTEM_PROMPT}\n\n{schema_block}", max_tokens=4096
        )
        for r in result.responses:
            self.meter.calls += 1
            self.meter.input_tokens += r.usage.input_tokens
            self.meter.output_tokens += r.usage.output_tokens
            self.meter.cost_usd += cost_usd(r.provider, r.model, r.usage) or 0.0
            self.meter.latency_s += r.latency_s
        return result.value

    def generate(self, question: str, schema: str, dialect: str) -> SQLCandidate:
        return self._parse(
            f"<question>{question}</question>",
            ClaudeSQLGenerator._schema_block(schema, dialect),
            SQLCandidate,
        )

    def repair(self, question, schema, dialect, attempts) -> SQLRepair:
        history = json.dumps(attempts, indent=2, default=str)
        prompt = (
            f"<question>{question}</question>\n\n<failed_attempts>\n{history}\n</failed_attempts>\n\n"
            "Every attempt above failed with the error shown. Diagnose the most recent failure against "
            "the schema, then write a corrected query. Do not repeat a query that already failed."
        )
        return self._parse(prompt, ClaudeSQLGenerator._schema_block(schema, dialect), SQLRepair)


def make_generator(spec: str | None = None) -> SQLGenerator:
    """``claude`` / unset -> native Anthropic generator; ``provider:model`` -> any agentkit provider."""
    spec = spec or os.environ.get("TEXT2SQL_MODEL")
    if spec and ":" in spec:
        from agentkit import load_model

        return LLMSQLGenerator(load_model(spec))
    return ClaudeSQLGenerator(model=None if spec in (None, "claude") else spec)


@dataclass
class ScriptedGenerator:
    """Replays fixed SQL: the first entry for ``generate``, the rest for successive repairs.

    Used by tests and by the eval harness's offline smoke mode.
    """

    script: dict[str, list[str]]
    meter: UsageMeter = field(default_factory=UsageMeter)

    def _next(self, question: str, n: int) -> str:
        sqls = self.script[question]
        return sqls[min(n, len(sqls) - 1)]

    def generate(self, question, schema, dialect) -> SQLCandidate:
        self.meter.record("scripted", 0, 0, 0.0)
        return SQLCandidate(reasoning="scripted", sql=self._next(question, 0))

    def repair(self, question, schema, dialect, attempts) -> SQLRepair:
        self.meter.record("scripted", 0, 0, 0.0)
        return SQLRepair(diagnosis=attempts[-1]["error"], sql=self._next(question, len(attempts)))
