"""Traer las respuestas de los hilos (migración 0032), la mitad que las guarda.

Las reglas que se vigilan aquí: solo se piden los hilos con respuestas nuevas; lo de
más de 24 horas entra como `archivado` (contexto, nunca research); el barrido de hilos
viejos no guarda jamás los mensajes principales que lee; y el lector real solo llama a
métodos de la lista blanca.
"""

import pytest

from handoff_agent import db
from handoff_agent import slack_client as sc
from handoff_agent.ingest import watcher
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader
from tests.test_slack_client import JAMAS, READ_METHODS, FakeWebClient

NOW = 1_726_000_000.0
DIA = 86400


def ts(seconds_ago: float) -> str:
    return f"{NOW - seconds_ago:.6f}"


@pytest.fixture
def reader():
    return FakeReader()


@pytest.fixture
def hilos(restore_config):
    """Con `leer_hilos` encendido; restore_config deja la tabla como estaba."""
    db.execute("update config set value = 'true'::jsonb where key = 'leer_hilos'")


def ajustar(clave: str, valor: int) -> None:
    db.execute("update config set value = %s::jsonb where key = %s", (str(valor), clave))


def tick(reader, channels=("C1",), at=NOW, **kwargs):
    return watcher.watch_tick(reader, list(channels), lookback_hours=1, now=lambda: at, **kwargs)


def fila(ts_: str, channel: str = "C1") -> dict | None:
    return db.fetch_one(
        "select user_id, thread_ts, status from slack_messages where channel_id = %s and ts = %s",
        (channel, ts_),
    )


def marcador(thread_ts: str, channel: str = "C1") -> str | None:
    f = db.fetch_one(
        "select ultima_respuesta from hilos where channel_id = %s and thread_ts = %s",
        (channel, thread_ts),
    )
    return f and f["ultima_respuesta"]


def barridos(reader, at=NOW, dias=7) -> int:
    """Cuántas veces se leyó el historial de la semana: eso es el barrido."""
    return sum(1 for _, oldest in reader.history_calls if oldest == f"{at - dias * DIA:.6f}")


# --- lo básico: el hilo de la página recién leída ---


def test_parent_and_replies_are_stored_with_the_parent_ts(conn, reader, hilos):
    reader.post("C1", "U1", "who's using a fractional CFO?", ts(600))
    reader.reply("C1", ts(600), "U2", "we need this too", ts(300))
    reader.reply("C1", ts(600), "U3", "+1", ts(200))
    result = tick(reader)
    assert reader.replies_calls == [("C1", ts(600), None)]
    assert (result.messages_new, result.threads_read, result.replies_new) == (1, 1, 2)
    assert fila(ts(600)) == {"user_id": "U1", "thread_ts": ts(600), "status": "nuevo"}
    assert fila(ts(300)) == {"user_id": "U2", "thread_ts": ts(600), "status": "nuevo"}
    assert fila(ts(200)) == {"user_id": "U3", "thread_ts": ts(600), "status": "nuevo"}
    assert marcador(ts(600)) == ts(200)


def test_replies_are_stored_oldest_first(conn, reader, hilos, monkeypatch):
    reader.post("C1", "U1", "p", ts(600))
    reader.reply("C1", ts(600), "U3", "segunda", ts(100))
    reader.reply("C1", ts(600), "U2", "primera", ts(300))
    guardados: list[str] = []
    real = watcher._store

    def espia(channel, message, status):
        guardados.append(message["ts"])
        return real(channel, message, status)

    monkeypatch.setattr(watcher, "_store", espia)
    tick(reader)
    # El principal, por la lectura normal; luego el hilo: principal y respuestas en orden.
    assert guardados == [ts(600), ts(600), ts(300), ts(100)]


def test_only_threads_with_a_newer_latest_reply_are_fetched(conn, reader, hilos):
    reader.post("C1", "U1", "al día", ts(900))
    reader.reply("C1", ts(900), "U2", "vieja", ts(800))
    reader.post("C1", "U1", "con novedades", ts(700))
    reader.reply("C1", ts(700), "U3", "nueva", ts(100))
    reader.post("C1", "U1", "sin hilo", ts(500))
    # El primer hilo ya se trajo hasta su última respuesta.
    db.execute(
        "insert into hilos (channel_id, thread_ts, ultima_respuesta) values ('C1', %s, %s)",
        (ts(900), ts(800)),
    )
    tick(reader)
    assert reader.replies_calls == [("C1", ts(700), None)]


def test_a_known_thread_is_asked_only_for_what_is_new(conn, reader, hilos):
    reader.post("C1", "U1", "p", ts(600))
    reader.reply("C1", ts(600), "U2", "primera", ts(500))
    tick(reader)
    reader.reply("C1", ts(600), "U3", "segunda", ts(100))
    # Pasada la media hora, el barrido ve que el hilo tiene una respuesta más nueva.
    result = tick(reader, at=NOW + 31 * 60)
    assert reader.replies_calls[-1] == ("C1", ts(600), ts(500))
    assert result.replies_new == 1
    assert marcador(ts(600)) == ts(100)
    assert db.fetch_one("select count(*) as n from slack_messages")["n"] == 3


def test_a_second_tick_fetches_nothing_new(conn, reader, hilos):
    reader.post("C1", "U1", "p", ts(600))
    reader.reply("C1", ts(600), "U2", "+1", ts(300))
    tick(reader)
    assert len(reader.replies_calls) == 1
    # Enseguida: ni página nueva ni barrido.
    second = tick(reader, at=NOW + 60)
    # Y pasada la media hora el barrido ve el hilo, pero ya está al día.
    third = tick(reader, at=NOW + 31 * 60)
    assert len(reader.replies_calls) == 1
    assert (second.threads_read, third.threads_read) == (0, 0)
    assert barridos(reader, at=NOW + 31 * 60) == 1


# --- lo viejo es contexto ---


def test_replies_older_than_24_hours_are_archived_and_fresh_ones_are_new(conn, reader, hilos):
    """La regla de "sin histórico": el resolver solo investiga `nuevo`."""
    reader.post("C1", "U1", "hace tres días", ts(3 * DIA))
    reader.reply("C1", ts(3 * DIA), "U2", "hace dos días", ts(2 * DIA))
    reader.reply("C1", ts(3 * DIA), "U3", "hace un rato", ts(600))
    result = tick(reader)
    assert fila(ts(2 * DIA))["status"] == "archivado"
    assert fila(ts(600)) == {"user_id": "U3", "thread_ts": ts(3 * DIA), "status": "nuevo"}
    assert result.replies_new == 1


def test_a_parent_the_read_left_out_is_archived_even_if_recent(conn, reader, hilos):
    """Un principal que la lectura normal no guardó es anterior a su ventana (aquí, una
    hora): entra como contexto. Como `nuevo`, el resolver investigaría a quien la
    lectura dejó fuera adrede."""
    reader.post("C1", "U1", "hace cinco horas", ts(5 * 3600))
    reader.reply("C1", ts(5 * 3600), "U2", "me too", ts(600))
    tick(reader)
    assert fila(ts(5 * 3600)) == {"user_id": "U1", "thread_ts": ts(5 * 3600), "status": "archivado"}
    assert fila(ts(600))["status"] == "nuevo"


def test_a_parent_already_stored_keeps_its_status(conn, reader, hilos):
    reader.post("C1", "U1", "p", ts(600))
    tick(reader)
    reader.reply("C1", ts(600), "U2", "+1", ts(60))
    tick(reader, at=NOW + 31 * 60)
    assert fila(ts(600))["status"] == "nuevo"


# --- el barrido de hilos viejos ---


def test_the_sweep_finds_replies_on_older_parents_and_never_stores_top_level_posts(
    conn, reader, hilos
):
    reader.post("C1", "U1", "viejo sin hilo", ts(2 * DIA))
    reader.post("C1", "U4", "viejo con hilo al día", ts(3 * DIA))
    reader.reply("C1", ts(3 * DIA), "U5", "vieja", ts(3 * DIA - 60))
    db.execute(
        "insert into hilos (channel_id, thread_ts, ultima_respuesta) values ('C1', %s, %s)",
        (ts(3 * DIA), ts(3 * DIA - 60)),
    )
    reader.post("C1", "U1", "viejo con respuesta nueva", ts(4 * DIA))
    reader.reply("C1", ts(4 * DIA), "U2", "+1", ts(300))
    result = tick(reader)
    assert barridos(reader) == 1
    assert reader.replies_calls == [("C1", ts(4 * DIA), None)]
    # Lo único guardado es el hilo que se pidió: ni el principal sin hilo, ni el del hilo
    # al día, entran por haberlos leído el barrido.
    assert fila(ts(2 * DIA)) is None
    assert fila(ts(3 * DIA)) is None
    assert fila(ts(4 * DIA))["status"] == "archivado"
    assert (result.messages_new, result.replies_new) == (0, 1)
    nuevos = db.fetch_all("select user_id from slack_messages where status = 'nuevo'")
    assert nuevos == [{"user_id": "U2"}]


def test_the_sweep_looks_back_the_configured_days(conn, reader, hilos):
    ajustar("hilos_dias", 2)
    reader.post("C1", "U1", "hace tres días", ts(3 * DIA))
    reader.reply("C1", ts(3 * DIA), "U2", "+1", ts(300))
    tick(reader)
    assert barridos(reader, dias=2) == 1
    assert reader.replies_calls == []


def test_the_sweep_runs_at_most_every_30_minutes(conn, reader, hilos):
    tick(reader)
    assert barridos(reader) == 1
    tick(reader, at=NOW + 29 * 60)
    assert barridos(reader, at=NOW + 29 * 60) == 0
    tick(reader, at=NOW + 31 * 60)
    assert barridos(reader, at=NOW + 31 * 60) == 1
    hecho = db.fetch_one("select extract(epoch from hilos_barrido_at) as t from canales")["t"]
    assert float(hecho) == pytest.approx(NOW + 31 * 60)


def test_a_channel_without_a_canales_row_is_not_swept(conn, reader, hilos, monkeypatch):
    monkeypatch.setattr(watcher, "_refrescar_nombre", lambda reader, channel: None)
    reader.post("C1", "U1", "viejo", ts(3 * DIA))
    reader.reply("C1", ts(3 * DIA), "U2", "+1", ts(300))
    reader.post("C1", "U1", "reciente", ts(600))
    reader.reply("C1", ts(600), "U3", "+1", ts(100))
    tick(reader)
    assert barridos(reader) == 0
    # El hilo de la página leída sí se trae: eso no necesita la fila.
    assert reader.replies_calls == [("C1", ts(600), None)]


def test_a_parent_newer_than_the_read_is_left_for_the_normal_read(conn, hilos):
    """Un principal que llega entre la lectura y el barrido no lo guarda el hilo: la
    lectura normal lo encontraría ya guardado y nunca lo contaría como `nuevo`."""

    class Tardio(FakeReader):
        def history(self, channel, oldest):
            mensajes = super().history(channel, oldest)
            if len(self.history_calls) == 1:  # tras la lectura normal, llega uno
                self.post("C1", "U7", "recién llegado", ts(30))
                self.reply("C1", ts(30), "U8", "+1", ts(20))
            return mensajes

    reader = Tardio()
    reader.post("C1", "U1", "leído", ts(600))
    tick(reader)
    assert reader.replies_calls == []
    assert fila(ts(30)) is None
    result = tick(reader, at=NOW + 60)
    assert fila(ts(30))["status"] == "nuevo"
    assert result.messages_new == 1 and result.replies_new == 1


# --- el tope por vuelta ---


def test_the_per_tick_cap_takes_the_most_recent_and_the_rest_wait(conn, reader, hilos):
    ajustar("hilos_por_vuelta", 2)
    for i, (parent, reply) in enumerate([(900, 400), (800, 100), (700, 300)]):
        reader.post("C1", "U1", f"p{i}", ts(parent))
        reader.reply("C1", ts(parent), f"U{i + 2}", "+1", ts(reply))
    result = tick(reader)
    assert [t for _, t, _ in reader.replies_calls] == [ts(800), ts(700)]
    assert result.threads_read == 2
    # El que no entró sale en la vuelta siguiente, aunque no toque barrido por tiempo.
    tick(reader, at=NOW + 60)
    assert [t for _, t, _ in reader.replies_calls] == [ts(800), ts(700), ts(900)]
    # Y con todo traído, el barrido vuelve a su media hora.
    tick(reader, at=NOW + 120)
    assert len(reader.replies_calls) == 3
    assert barridos(reader, at=NOW + 120) == 0


def test_the_cap_is_shared_across_channels(conn, reader, hilos):
    ajustar("hilos_por_vuelta", 1)
    reader.post("C1", "U1", "p", ts(900))
    reader.reply("C1", ts(900), "U2", "+1", ts(500))
    reader.post("C2", "U1", "p", ts(800))
    reader.reply("C2", ts(800), "U3", "+1", ts(100))
    tick(reader, channels=("C1", "C2"))
    assert reader.replies_calls == [("C2", ts(800), None)]


def test_a_zero_cap_reads_no_threads(conn, reader, hilos):
    ajustar("hilos_por_vuelta", 0)
    reader.post("C1", "U1", "p", ts(600))
    reader.reply("C1", ts(600), "U2", "+1", ts(300))
    tick(reader)
    assert reader.replies_calls == []
    assert barridos(reader) == 0


# --- quién escribe y qué pasa cuando algo falla ---


@pytest.mark.parametrize(
    ("user", "extra"),
    [
        ("UOWNER", {}),
        ("U1", {"bot_id": "B1"}),
        ("U1", {"subtype": "bot_message"}),
        (None, {}),
    ],
)
def test_owner_bot_and_system_replies_are_ignored(conn, reader, hilos, user, extra):
    reader.post("C1", "UOWNER", "who needs X?", ts(600))
    reader.reply("C1", ts(600), user, "respuesta", ts(300), **extra)
    result = tick(reader)
    assert fila(ts(300))["status"] == "ignorado"
    assert fila(ts(600))["status"] == "ignorado"
    assert result.replies_new == 0


def test_one_failing_thread_does_not_stop_the_others(conn, reader, hilos):
    reader.post("C1", "U1", "falla", ts(900))
    reader.reply("C1", ts(900), "U2", "+1", ts(100))
    reader.post("C1", "U1", "funciona", ts(800))
    reader.reply("C1", ts(800), "U3", "+1", ts(200))
    reader.unavailable_threads.add(ts(900))
    result = tick(reader)
    assert len(result.errors) == 1 and ts(900) in result.errors[0]
    assert result.threads_read == 1 and result.channels_read == 1
    assert fila(ts(200))["status"] == "nuevo"
    assert marcador(ts(900)) is None  # se vuelve a pedir más adelante
    assert marcador(ts(800)) == ts(200)


def test_a_dead_token_while_reading_a_thread_propagates(conn, reader, hilos, monkeypatch):
    reader.post("C1", "U1", "p", ts(600))
    reader.reply("C1", ts(600), "U2", "+1", ts(300))

    def caducado(channel, thread_ts, oldest=None):
        raise SlackAuthFailed("Slack rechazó el token: token_revoked")

    monkeypatch.setattr(reader, "respuestas", caducado)
    with pytest.raises(SlackAuthFailed):
        tick(reader)


def test_a_failing_sweep_does_not_lose_the_channel_read(conn, reader, hilos):
    class BarridoRoto(FakeReader):
        def history(self, channel, oldest):
            if self.history_calls:  # la segunda llamada es el barrido
                self.history_calls.append((channel, oldest))
                raise sc.SlackUnavailable("conversations_history: internal_error")
            return super().history(channel, oldest)

    reader = BarridoRoto()
    reader.post("C1", "U1", "hola", ts(600))
    result = tick(reader)
    assert result.messages_new == 1 and result.channels_read == 1
    assert len(result.errors) == 1
    # Sin apuntar: se vuelve a barrer en la vuelta siguiente.
    assert db.fetch_one("select hilos_barrido_at from canales")["hilos_barrido_at"] is None


def test_threads_are_read_in_the_rotating_mode_too(conn, reader, hilos):
    db.execute(
        "insert into canales (channel_id, nombre, es_miembro, actualizado_at) "
        "values ('C1', 'general', true, now())"
    )
    reader.post("C1", "U1", "p", ts(600))
    reader.reply("C1", ts(600), "U2", "+1", ts(300))
    result = tick(reader, rotando=True)
    assert result.replies_new == 1
    assert barridos(reader) == 1


# --- el lector real y la lista blanca ---


class FakeWebClientConHilos(FakeWebClient):
    """El doble del WebClient de test_slack_client, con conversations.replies."""

    def __init__(self, *, reply_pages=(), **kwargs):
        super().__init__(**kwargs)
        self.reply_pages = list(reply_pages)

    def conversations_history(self, **kwargs):
        self._record("conversations_history", kwargs)
        if self.history_pages:
            return self.history_pages.pop(0)
        return {"ok": True, "messages": [], "has_more": False}

    def conversations_members(self, **kwargs):
        self._record("conversations_members", kwargs)
        return {"ok": True, "members": [], "response_metadata": {}}

    def conversations_replies(self, **kwargs):
        self._record("conversations_replies", kwargs)
        return self.reply_pages.pop(0)


def test_respuestas_follows_cursors_and_passes_the_watermark():
    fake = FakeWebClientConHilos(
        reply_pages=[
            {
                "ok": True,
                "messages": [{"ts": "1.0"}, {"ts": "2.0"}],
                "has_more": True,
                "response_metadata": {"next_cursor": "abc"},
            },
            {"ok": True, "messages": [{"ts": "3.0"}], "has_more": False},
        ]
    )
    reader = sc.FoundersClubReader(client=fake)
    assert [m["ts"] for m in reader.respuestas("C1", "1.0", oldest="1.5")] == ["1.0", "2.0", "3.0"]
    nombre, primera = fake.calls[0]
    assert nombre == "conversations_replies"
    assert primera["channel"] == "C1" and primera["ts"] == "1.0" and primera["oldest"] == "1.5"
    assert fake.calls[1][1]["cursor"] == "abc"


def test_reading_threads_end_to_end_only_calls_whitelisted_methods(conn, hilos):
    """El lector real, de punta a punta con hilos y barrido: nada fuera de la lista
    blanca, nada de JAMAS, y ni rastro de conversations.join ni conversations.mark."""
    padre = {
        "ts": ts(600),
        "user": "U1",
        "text": "who needs X?",
        "thread_ts": ts(600),
        "reply_count": 1,
        "latest_reply": ts(300),
    }
    fake = FakeWebClientConHilos(
        history_pages=[{"ok": True, "messages": [padre], "has_more": False}],
        reply_pages=[
            {
                "ok": True,
                "messages": [padre, {"ts": ts(300), "user": "U2", "thread_ts": ts(600)}],
                "has_more": False,
            }
        ],
    )
    reader = sc.FoundersClubReader(client=fake)
    result = tick(reader)
    assert result.replies_new == 1
    llamados = {name for name, _ in fake.calls}
    assert "conversations_replies" in llamados
    assert llamados <= READ_METHODS
    assert not (llamados & JAMAS)
    assert fila(ts(300)) == {"user_id": "U2", "thread_ts": ts(600), "status": "nuevo"}
