import pytest

from handoff_agent import db
from handoff_agent.ingest import queue, watcher
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader

NOW = 1_726_000_000.0


def ts(seconds_ago: float) -> str:
    return f"{NOW - seconds_ago:.6f}"


@pytest.fixture
def reader():
    return FakeReader()


def tick(reader, channels=("C1",)):
    return watcher.watch_tick(reader, list(channels), lookback_hours=1, now=lambda: NOW)


def test_the_first_read_only_looks_back_the_configured_hours(conn, reader):
    """Sin backfill: lo de antes de la ventana no se lee."""
    reader.post("C1", "U1", "reciente", ts(600))
    reader.post("C1", "U2", "de hace dos horas", ts(7200))
    assert tick(reader).messages_new == 1
    assert db.fetch_one("select user_id from slack_messages")["user_id"] == "U1"


def test_later_reads_start_after_the_last_stored_message(conn, reader):
    reader.post("C1", "U1", "a", ts(600))
    tick(reader)
    reader.post("C1", "U2", "b", ts(300))
    assert tick(reader).messages_new == 1
    assert reader.history_calls[-1][1] == ts(600)


@pytest.mark.parametrize(
    ("user", "extra"),
    [
        ("U1", {"bot_id": "B1"}),
        ("U1", {"subtype": "channel_join"}),
        ("U1", {"subtype": "bot_message"}),
        ("UOWNER", {}),
    ],
)
def test_bots_system_messages_and_the_owner_are_ignored(conn, reader, user, extra):
    reader.post("C1", user, "x", ts(60), **extra)
    result = tick(reader)
    assert (result.messages_new, result.messages_ignored) == (0, 1)
    assert db.fetch_one("select status from slack_messages")["status"] == "ignorado"


def test_a_message_with_a_file_is_a_real_message(conn, reader):
    reader.post("C1", "U1", "mira esto", ts(60), subtype="file_share")
    assert tick(reader).messages_new == 1


def test_the_first_member_list_is_a_baseline_and_queues_nobody(conn, reader):
    """El primer padrón nunca encola a todos los miembros: sería el backfill
    de 1.400 personas que se descartó."""
    reader.set_members("C1", "U1", "U2", "U3")
    assert tick(reader).members_new == 0
    assert queue.status_counts() == {}
    assert db.fetch_one("select count(*) as n from member_snapshots")["n"] == 1


def test_new_members_are_queued_on_later_reads(conn, reader):
    reader.set_members("C1", "U1")
    tick(reader)
    reader.set_members("C1", "U1", "U2", "UOWNER")
    assert tick(reader).members_new == 1
    job = db.fetch_one("select slack_user_id, reason from research_jobs")
    assert job == {"slack_user_id": "U2", "reason": "miembro_nuevo"}


def test_messages_are_stored_oldest_first(conn, reader, monkeypatch):
    """Slack devuelve primero lo más nuevo y cada mensaje se confirma por
    separado, así que hay que guardar de viejo a nuevo: el marcador de
    reanudación es el ts más alto guardado."""
    reader.post("C1", "U1", "viejo", ts(600))
    reader.post("C1", "U2", "nuevo", ts(60))
    stored: list[str] = []
    real_store = watcher._store

    def spy(channel, message, status):
        stored.append(message["ts"])
        return real_store(channel, message, status)

    monkeypatch.setattr(watcher, "_store", spy)
    tick(reader)
    assert stored == [ts(600), ts(60)]


def test_an_interrupted_batch_does_not_lose_the_older_messages(conn, reader, monkeypatch):
    """Un parpadeo de Postgres a media tanda no puede dejar mensajes fuera de
    todas las lecturas futuras."""
    reader.post("C1", "U1", "viejo", ts(600))
    reader.post("C1", "U2", "nuevo", ts(60))
    real_store = watcher._store
    calls = {"n": 0}

    def flaky(channel, message, status):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Postgres parpadeó")
        return real_store(channel, message, status)

    monkeypatch.setattr(watcher, "_store", flaky)
    with pytest.raises(RuntimeError):
        tick(reader)
    monkeypatch.undo()
    tick(reader)
    stored = {row["user_id"] for row in db.fetch_all("select user_id from slack_messages")}
    assert stored == {"U1", "U2"}


def test_an_interrupted_member_diff_does_not_lose_the_stragglers(conn, reader, monkeypatch):
    """La instantánea del padrón se guarda al final: si se corta a media lista,
    la vuelta siguiente tiene que volver a ver a los que faltaban."""
    reader.set_members("C1", "U1")
    tick(reader)
    reader.set_members("C1", "U1", "U2", "U3")
    real_enqueue = queue.enqueue
    calls = {"n": 0}

    def flaky(user_id, reason):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Postgres parpadeó")
        return real_enqueue(user_id, reason)

    monkeypatch.setattr(watcher.queue, "enqueue", flaky)
    with pytest.raises(RuntimeError):
        tick(reader)
    monkeypatch.undo()
    tick(reader)
    queued = {
        row["slack_user_id"] for row in db.fetch_all("select slack_user_id from research_jobs")
    }
    assert queued == {"U2", "U3"}


def test_one_channel_down_does_not_stop_the_others(conn, reader):
    reader.unavailable_channels.add("C1")
    reader.post("C2", "U1", "hola", ts(60))
    result = tick(reader, channels=("C1", "C2"))
    assert result.messages_new == 1
    assert len(result.errors) == 1 and "C1" in result.errors[0]


def test_an_auth_failure_propagates(conn, reader):
    reader.auth_error = SlackAuthFailed("Slack rechazó el token: token_revoked")
    with pytest.raises(SlackAuthFailed):
        tick(reader)


# --- nombres de canal (tabla `canales`, migración 0025) ---


def nombre_guardado(channel="C1"):
    fila = db.fetch_one("select nombre from canales where channel_id = %s", (channel,))
    return fila and fila["nombre"]


def test_the_cycle_saves_the_channel_name(conn, reader):
    reader.channel_names["C1"] = "founders-general"
    reader.post("C1", "U1", "hola", ts(60))
    assert tick(reader).messages_new == 1
    assert nombre_guardado() == "founders-general"
    assert reader.name_calls == ["C1"]


def test_the_name_is_not_asked_again_within_a_day(conn, reader):
    reader.channel_names["C1"] = "founders-general"
    tick(reader)
    tick(reader)
    tick(reader)
    assert reader.name_calls == ["C1"]
    # Pasado el día, se vuelve a preguntar (y un canal renombrado se actualiza).
    db.execute("update canales set actualizado_at = now() - interval '25 hours'")
    reader.channel_names["C1"] = "founders-privado"
    tick(reader)
    assert reader.name_calls == ["C1", "C1"]
    assert nombre_guardado() == "founders-privado"


def test_a_channel_without_a_name_in_slack_does_not_block_the_read(conn, reader):
    reader.post("C1", "U1", "hola", ts(60))
    assert tick(reader).messages_new == 1  # channel_names vacío: Slack no da nombre
    assert nombre_guardado() is None
    assert db.fetch_one("select count(*) as n from canales")["n"] == 1


def test_if_slack_fails_the_read_goes_on_without_a_name(conn, reader, caplog):
    reader.unavailable_channels.add("C2")
    reader.channel_names["C2"] = "nunca-se-sabra"
    reader.post("C1", "U1", "hola", ts(60))
    reader.channel_names["C1"] = "founders-general"
    with caplog.at_level("WARNING"):
        result = tick(reader, channels=("C1", "C2"))
    # C1 se lee y se nombra; de C2 solo se queda el intento anotado, sin nombre.
    assert result.messages_new == 1 and result.channels_read == 1
    assert nombre_guardado("C1") == "founders-general"
    assert nombre_guardado("C2") is None
    assert any("sin nombre para el canal C2" in r.getMessage() for r in caplog.records)
    # Y no se insiste en el siguiente sondeo: el intento cuenta como consulta.
    tick(reader, channels=("C1", "C2"))
    assert reader.name_calls.count("C2") == 1


def test_an_existing_name_survives_a_failure(conn, reader):
    reader.channel_names["C1"] = "founders-general"
    tick(reader)
    db.execute("update canales set actualizado_at = now() - interval '25 hours'")
    reader.unavailable_channels.add("C1")
    tick(reader)
    assert nombre_guardado() == "founders-general"


def test_a_dead_token_still_stops_the_read(conn, reader):
    reader.auth_error = SlackAuthFailed("token_revoked")
    with pytest.raises(SlackAuthFailed):
        tick(reader)
    assert db.fetch_one("select count(*) as n from canales")["n"] == 0


# --- el marcador de lectura y los hilos (migración 0032) ---


def test_a_stored_reply_does_not_push_the_read_past_unread_posts(conn, reader):
    """Regresión: el marcador de lectura es el último mensaje PRINCIPAL guardado. Una
    respuesta reciente a un hilo (thread_ts distinto de su ts) no puede adelantarlo: la
    lectura siguiente empezaría después de mensajes principales que nadie ha leído."""
    reader.post("C1", "U1", "principal", ts(600))
    tick(reader)
    # Una respuesta guardada de un hilo, más nueva que lo que falta por leer.
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, thread_ts, status) "
        "values ('C1', %s, 'U9', '+1', %s, 'nuevo')",
        (ts(60), ts(600)),
    )
    reader.post("C1", "U2", "sin leer todavía", ts(300))
    assert tick(reader).messages_new == 1
    assert reader.history_calls[-1][1] == ts(600)
    assert db.fetch_one("select 1 as x from slack_messages where ts = %s", (ts(300),))


def test_a_stored_parent_still_counts_for_the_read_watermark(conn, reader):
    """El principal de un hilo se guarda con thread_ts = ts, y sí cuenta como leído."""
    reader.post(
        "C1", "U1", "con hilo", ts(600), thread_ts=ts(600), reply_count=1, latest_reply=ts(500)
    )
    tick(reader)
    tick(reader)
    assert reader.history_calls[-1][1] == ts(600)


def test_with_threads_off_nothing_reads_replies_or_sweeps(conn, reader):
    """`leer_hilos` nace apagado: ni conversations.replies ni el barrido de una semana."""
    reader.post("C1", "U1", "who needs X?", ts(600))
    reader.reply("C1", ts(600), "U2", "+1, we need this too", ts(300))
    result = tick(reader)
    assert reader.replies_calls == []
    assert len(reader.history_calls) == 1  # solo la lectura normal
    assert (result.threads_read, result.replies_new) == (0, 0)
    assert db.fetch_one("select count(*) as n from slack_messages")["n"] == 1
    assert db.fetch_one("select count(*) as n from hilos")["n"] == 0
