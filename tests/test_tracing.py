from handoff_agent import tracing


def test_tracing_is_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing.is_enabled() is False


def test_tracing_is_enabled_with_both_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    assert tracing.is_enabled() is True


def test_trace_llm_call_is_a_noop_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing.trace_llm_call(
        stage="triage", model="gpt-4.1", prompt="hola", completion="adios",
        usage={"input": 1, "cached": 0, "output": 1}, latency_ms=10,
    ) is None


def test_llm_complete_still_works_when_tracing_is_off(conn, monkeypatch):
    """El trazado nunca puede tumbar una llamada: Supabase es la fuente de verdad."""
    from handoff_agent import llm

    from tests.test_llm import FakeOpenAI

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI())
    assert llm.complete("hola", stage="triage").text == "hola"
