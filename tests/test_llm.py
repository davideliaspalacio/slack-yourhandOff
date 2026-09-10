from decimal import Decimal
from types import SimpleNamespace

import pytest

from handoff_agent import guards, llm


class FakeOpenAI:
    """Doble de la parte del SDK de OpenAI que usamos, y solo de esa parte."""

    def __init__(self, text="hola", input_tokens=1000, cached=0, output_tokens=500):
        self._text = text
        self._usage = SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        )
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._text))],
            usage=self._usage,
        )


def test_complete_returns_text_and_cost(conn, monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI(text="respuesta"))
    result = llm.complete("¿quién es Ada Lovelace?", stage="research")
    expected = Decimal(1000) / Decimal(1000000) * Decimal("2.00") + Decimal(500) / Decimal(
        1000000
    ) * Decimal("8.00")
    assert result.text == "respuesta"
    assert result.cost_usd == expected
    assert result.latency_ms >= 0


def test_complete_writes_one_row_to_the_ledger(conn, monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI())
    llm.complete("hola", stage="triage")
    with conn.cursor() as cur:
        cur.execute("select stage, input_tokens, output_tokens from llm_calls")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0] == ("triage", 1000, 500)


def test_complete_charges_cached_tokens_separately(conn, monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI(input_tokens=1000, cached=800))
    llm.complete("hola", stage="triage")
    with conn.cursor() as cur:
        cur.execute("select input_tokens, cached_tokens from llm_calls")
        assert cur.fetchone() == (200, 800)


def test_complete_refuses_to_run_when_the_kill_switch_is_on(conn, monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI())
    with conn.cursor() as cur:
        cur.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    try:
        with pytest.raises(guards.KillSwitchActive):
            llm.complete("hola", stage="triage")
    finally:
        with conn.cursor() as cur:
            cur.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")


def test_json_mode_asks_openai_for_a_json_object(conn, monkeypatch):
    fake = FakeOpenAI(text='{"ok": true}')
    monkeypatch.setattr(llm, "_client", lambda: fake)
    llm.complete("dame json", stage="triage", json_mode=True)
    assert fake.calls[0]["response_format"] == {"type": "json_object"}


def test_llm_raises_a_clear_error_when_the_openai_key_is_missing(conn, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm._client.cache_clear()
    try:
        with pytest.raises(llm.OpenAINotConfigured, match="OPENAI_API_KEY"):
            llm.complete("hola", stage="triage")
    finally:
        llm._client.cache_clear()
