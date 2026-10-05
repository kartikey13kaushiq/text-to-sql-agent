from types import SimpleNamespace

import pytest

from text2sql.llm import ClaudeSQLGenerator, SQLCandidate, SQLRepair


class FakeMessages:
    def __init__(self, parsed):
        self.parsed, self.calls = parsed, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            parsed_output=self.parsed,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=2_000, output_tokens=100),
        )


def test_schema_is_a_cached_system_block():
    messages = FakeMessages(SQLCandidate(reasoning="r", sql="SELECT 1"))
    gen = ClaudeSQLGenerator(model="claude-opus-5", client=SimpleNamespace(messages=messages))
    assert gen.generate("q?", "CREATE TABLE t (a INT);", "sqlite").sql == "SELECT 1"
    system = messages.calls[0]["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    assert "CREATE TABLE t" in system[-1]["text"]
    assert messages.calls[0]["output_format"] is SQLCandidate


def test_repair_prompt_carries_every_failed_attempt():
    messages = FakeMessages(SQLRepair(diagnosis="d", sql="SELECT 2"))
    gen = ClaudeSQLGenerator(model="claude-opus-5", client=SimpleNamespace(messages=messages))
    attempts = [
        {"sql": "SELECT a FROM x", "error_type": "guard", "error": "unknown table(s): x"},
        {"sql": "SELECT b FROM t", "error_type": "unknown_identifier", "error": "no such column: b"},
    ]
    gen.repair("q?", "schema", "sqlite", attempts)
    prompt = messages.calls[0]["messages"][0]["content"]
    assert "unknown table(s): x" in prompt and "no such column: b" in prompt
    assert gen.meter.calls == 1


def test_provider_agnostic_generator_with_any_agentkit_model():
    pytest.importorskip("agentkit", reason="install agentkit-core for the provider-agnostic backend")
    from agentkit import ScriptedModel

    from text2sql.llm import LLMSQLGenerator

    model = ScriptedModel(
        [
            {"reasoning": "count rows", "sql": "SELECT COUNT(*) FROM t"},
            {"diagnosis": "wrong table", "sql": "SELECT COUNT(*) FROM t2"},
        ]
    )
    gen = LLMSQLGenerator(model)
    assert gen.generate("how many?", "CREATE TABLE t (a INT);", "sqlite").sql == "SELECT COUNT(*) FROM t"
    assert "CREATE TABLE t" in model.requests[0]["system"]
    repair = gen.repair("how many?", "schema", "sqlite", [{"sql": "x", "error_type": "guard", "error": "boom"}])
    assert repair.sql.endswith("t2") and "boom" in model.requests[1]["messages"][0].content
    assert gen.meter.calls == 2 and gen.model == "scripted:scripted"
