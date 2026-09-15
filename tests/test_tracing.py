import json
import time
from types import SimpleNamespace

import pytest
from langfuse import Langfuse
from langfuse import LangfuseOtelSpanAttributes as A
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from handoff_agent import llm, tracing
from tests.test_llm import FakeOpenAI

# Un solo cliente y un solo exportador para toda la sesión. El SDK 4 comparte el
# primer exportador entre los clientes con la misma clave pública: con un cliente
# nuevo por test, las trazas de un test acabarían en el exportador de otro.
EXPORTER = InMemorySpanExporter()
CLIENT = Langfuse(
    public_key="pk-lf-test",
    secret_key="sk-lf-test",
    base_url="http://127.0.0.1:9",  # inalcanzable: ningún test llega a la red
    span_exporter=EXPORTER,
)


@pytest.fixture
def langfuse_on(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setattr(tracing, "_client", lambda: CLIENT)
    EXPORTER.clear()
    yield
    EXPORTER.clear()


def exported():
    CLIENT.flush()
    return EXPORTER.get_finished_spans()


def trace_id_of(span) -> str:
    return format(span.context.trace_id, "032x")


def test_tracing_is_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing.is_enabled() is False


def test_tracing_is_enabled_with_both_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    assert tracing.is_enabled() is True


def test_without_keys_nothing_is_traced(monkeypatch):
    monkeypatch.setattr(tracing, "_client", lambda: CLIENT)
    EXPORTER.clear()
    generation = tracing.start_generation(stage="triage", model="gpt-4.1", prompt="hola")
    assert generation is None
    assert tracing.finish_generation(generation, completion="adios") is None
    assert len(exported()) == 0


def test_a_finished_generation_carries_everything_langfuse_needs(langfuse_on):
    generation = tracing.start_generation(
        stage="research_sintesis", model="gpt-4.1", prompt="hola", prospect_id="p-1"
    )
    trace_id = tracing.finish_generation(
        generation, completion="adios", usage={"input": 100, "cached": 40, "output": 20}
    )
    (span,) = exported()
    attrs = span.attributes
    assert span.name == "research_sintesis"
    assert attrs[A.OBSERVATION_TYPE] == "generation"
    assert attrs[A.OBSERVATION_MODEL] == "gpt-4.1"
    assert attrs[A.OBSERVATION_INPUT] == "hola"
    assert attrs[A.OBSERVATION_OUTPUT] == "adios"
    assert json.loads(attrs[A.OBSERVATION_USAGE_DETAILS]) == {
        "input": 100,
        "cache_read_input_tokens": 40,
        "output": 20,
    }
    assert attrs[f"{A.OBSERVATION_METADATA}.prospect_id"] == "p-1"
    assert trace_id == trace_id_of(span)


def test_the_generation_lasts_as_long_as_the_call(langfuse_on):
    """Langfuse mide la latencia por la duración de la observación. Crearla una
    vez terminada la llamada, como hacía la versión rota, la dejaba en cero."""
    generation = tracing.start_generation(stage="s", model="gpt-4.1", prompt="hola")
    time.sleep(0.05)
    tracing.finish_generation(generation, completion="adios")
    (span,) = exported()
    assert (span.end_time - span.start_time) / 1e9 >= 0.05


def test_a_failed_call_is_exported_as_an_error(langfuse_on):
    generation = tracing.start_generation(stage="s", model="gpt-4.1", prompt="hola")
    trace_id = tracing.finish_generation(generation, error=RuntimeError("timeout de OpenAI"))
    (span,) = exported()
    assert span.attributes[A.OBSERVATION_LEVEL] == "ERROR"
    assert "RuntimeError" in span.attributes[A.OBSERVATION_STATUS_MESSAGE]
    assert "timeout de OpenAI" in span.attributes[A.OBSERVATION_STATUS_MESSAGE]
    assert trace_id == trace_id_of(span)


def test_a_client_that_cannot_start_never_breaks_the_caller(langfuse_on, monkeypatch):
    def boom():
        raise RuntimeError("Langfuse no arranca")

    monkeypatch.setattr(tracing, "_client", boom)
    assert tracing.start_generation(stage="s", model="gpt-4.1", prompt="hola") is None


def test_a_generation_that_cannot_close_never_breaks_the_caller(langfuse_on):
    class Broken:
        def update(self, **kwargs):
            raise RuntimeError("exportador caído")

        def end(self, **kwargs):
            raise RuntimeError("exportador caído")

    assert tracing.finish_generation(Broken(), completion="adios") is None


class SlowOpenAI(FakeOpenAI):
    def _create(self, **kwargs):
        time.sleep(0.05)
        return super()._create(**kwargs)


def test_llm_complete_stores_the_langfuse_trace_id_in_the_ledger(conn, langfuse_on, monkeypatch):
    fake = FakeOpenAI(text="respuesta", input_tokens=1000, cached=800, output_tokens=50)
    monkeypatch.setattr(llm, "_client", lambda: fake)
    llm.complete("hola", stage="research_sintesis")
    (span,) = exported()
    with conn.cursor() as cur:
        cur.execute("select trace_id from llm_calls")
        (stored,) = cur.fetchone()
    assert stored == trace_id_of(span)
    assert span.attributes[A.OBSERVATION_OUTPUT] == "respuesta"
    assert json.loads(span.attributes[A.OBSERVATION_USAGE_DETAILS]) == {
        "input": 200,
        "cache_read_input_tokens": 800,
        "output": 50,
    }


def test_llm_complete_opens_the_generation_before_calling_openai(conn, langfuse_on, monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: SlowOpenAI())
    llm.complete("hola", stage="s")
    (span,) = exported()
    assert (span.end_time - span.start_time) / 1e9 >= 0.05


def test_an_openai_failure_is_traced_and_still_raised(conn, langfuse_on, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("OpenAI caído")

    broken = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
    monkeypatch.setattr(llm, "_client", lambda: broken)
    with pytest.raises(RuntimeError, match="OpenAI caído"):
        llm.complete("hola", stage="s")
    (span,) = exported()
    assert span.attributes[A.OBSERVATION_LEVEL] == "ERROR"
    with conn.cursor() as cur:
        cur.execute("select count(*) from llm_calls")
        assert cur.fetchone()[0] == 0


def test_llm_complete_still_works_when_tracing_is_off(conn, monkeypatch):
    """El trazado nunca puede tumbar una llamada: Supabase es la fuente de verdad."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(llm, "_client", lambda: FakeOpenAI())
    assert llm.complete("hola", stage="triage").text == "hola"
