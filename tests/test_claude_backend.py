from types import SimpleNamespace

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
