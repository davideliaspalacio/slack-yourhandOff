"""Los recordatorios de lo que el seguimiento no cubría (0038): hilos de canal,
menciones y mensajes directos de gente sin ficha. Y sobre todo lo que NO avisan: lo que
él ya contestó, aunque la base todavía no lo sepa."""

import ast
import json
import time
from pathlib import Path

import psycopg
import pytest

from handoff_agent import db
from handoff_agent.delivery import seguimiento, seguimiento_canal, sugerencia
from handoff_agent.slack_client import SlackAuthFailed
from tests.slack_fakes import FakeReader
from tests.test_panel_rls import ALLOWED, STRANGER, as_user

OWNER = "UOWNER"
CANAL = "CGEN"
SECRETO = "necesitamos tres ingenieros para marzo"
INTERRUPTORES = (
    "seguimiento_hilos_activo",
    "seguimiento_menciones_activo",
    "seguimiento_sin_ficha_activo",
    "leer_hilos",
)


@pytest.fixture(autouse=True)
def slack(monkeypatch):
    """Bot configurado y `post_card` sustituido: lo publicado queda en la lista."""
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    posted = []

    def post_card(blocks, text):
        posted.append(
            {"blocks": blocks, "text": text, "json": json.dumps(blocks, ensure_ascii=False)}
        )
        return "CHANDOFF", f"1700000000.{len(posted):06d}"

    monkeypatch.setattr(seguimiento.slack_writer, "post_card", post_card)
    return posted


def ajuste(clave, valor):
    db.execute("update config set value = %s::jsonb where key = %s", (json.dumps(valor), clave))


def tocado_hace(clave, dias=30):
    """Como si el interruptor se hubiera tocado por última vez hace `dias` días: solo
    cuenta lo que llegó después."""
    db.execute(
        "update config set updated_at = now() - make_interval(days => %s) where key = %s",
        (dias, clave),
    )


@pytest.fixture
def reader(conn, restore_config):
    """El Slack del club, con los hilos encendidos y los interruptores tocados hace un
    mes (recién migrada la base nacen "ahora", y nada anterior contaría)."""
    ajuste("leer_hilos", True)
    for clave in INTERRUPTORES:
        tocado_hace(clave)
    db.execute("insert into canales (channel_id, nombre) values (%s, 'founders-general')", (CANAL,))
    return FakeReader(owner=OWNER)


def ts(horas: float) -> str:
    return f"{time.time() - horas * 3600:.6f}"


def persona(slack_user_id="USUNNY", nombre="Sunny Patel", empresa="Acme", estado="nuevo"):
    return db.fetch_one(
        "insert into prospects (slack_user_id, full_name, company_name, state) "
        "values (%s, %s, %s, %s) returning id",
        (slack_user_id, nombre, empresa, estado),
    )["id"]


def escribe(
    reader,
    user,
    horas,
    hilo=None,
    en_base=True,
    en_slack=True,
    texto=SECRETO,
    canal=CANAL,
    status=None,
    **extra,
):
    """Un mensaje de canal: en la base (lo que ya leyó el watcher) y en Slack (lo que hay
    de verdad). `hilo` es el `ts` del principal si es una respuesta. Con `en_base=False`
    es algo que Slack ya tiene y el watcher todavía no ha traído."""
    marca = ts(horas)
    if en_slack:
        if hilo:
            reader.reply(canal, hilo, user, texto, marca, **extra)
        else:
            reader.post(canal, user, texto, marca, **extra)
    if en_base:
        db.execute(
            "insert into slack_messages (channel_id, ts, user_id, text, thread_ts, status) "
            "values (%s, %s, %s, %s, %s, %s)",
            (canal, marca, user, texto, hilo, status or ("ignorado" if user == OWNER else "nuevo")),
        )
    return marca


def menciona(reader, user, horas, hilo=None, estado="publicada", **extra):
    """Alguien lo menciona, y `signals/eventos.py` ya lo apuntó en `eventos_canal`."""
    marca = escribe(reader, user, horas, hilo=hilo, texto=f"<@{OWNER}> {SECRETO}", **extra)
    db.execute(
        "insert into eventos_canal (channel_id, ts, tipo, slack_user_id, estado) "
        "values (%s, %s, 'mencion', %s, %s)",
        (CANAL, marca, user, estado),
    )
    return marca


def dm_sin_ficha(dm_id="DZZZ", user="UDANA", horas=30.0, de="otro"):
    db.execute(
        "insert into dm_rastro (dm_id, team_id, slack_user_id, ultimo_ts, ultimo_de, avisado_at) "
        "values (%s, 'T0OWNER', %s, %s, %s, now() - interval '2 days')",
        (dm_id, user, ts(horas), de),
    )
    return dm_id


def dm_de_lead(dm_id="DAAA", user="ULEAD", horas=30.0):
    pid = persona(user, "Lee Lead", "Leads Inc")
    db.execute(
        "insert into dm_rastro (dm_id, team_id, slack_user_id, prospect_id, ultimo_ts, ultimo_de) "
        "values (%s, 'T0OWNER', %s, %s, %s, 'otro')",
        (dm_id, user, pid, ts(horas)),
    )


def lineas(publicado):
    return publicado["blocks"][0]["text"]["text"].splitlines()


def apunte(clave_ts):
    return db.fetch_one(
        "select * from seguimiento_canal where channel_id = %s and clave_ts = %s", (CANAL, clave_ts)
    )


def enlace(marca):
    return f"<https://fake.slack.com/archives/{CANAL}/p{marca.replace('.', '')}|Open in Slack>"


def pasan_horas(horas):
    """El último aviso de cada hilo o mención, como si hubiera salido hace `horas`."""
    db.execute(
        "update seguimiento_canal set avisado_at = avisado_at - make_interval(hours => %s)",
        (horas,),
    )


# --- hilos ------------------------------------------------------------------------


def test_a_reply_in_a_thread_he_started_is_a_reminder(reader, slack):
    persona()
    raiz = escribe(reader, OWNER, 50)
    respuesta = escribe(reader, "USUNNY", 30, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert lineas(slack[0]) == [
        "🟡 *TODAY* · ⏳ *WAITING ON YOU*",
        "",
        "*Thread replies you haven't answered*",
        (
            "• *Sunny Patel* — Acme · Sunny replied in your thread in #founders-general "
            f"30 hours ago · {enlace(respuesta)}"
        ),
    ]
    fila = apunte(raiz)
    assert (fila["tipo"], fila["pendiente_ts"], fila["avisos"]) == ("hilo", respuesta, 1)


def test_a_reply_inside_the_deadline_waits(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 5, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    # Y sin plazo cumplido ni se le pregunta a Slack.
    assert reader.replies_calls == []


def test_the_deadline_comes_from_config(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 5, hilo=raiz)
    ajuste("seguimiento_hilo_horas", 4)
    assert seguimiento.publicar_avisos(reader=reader) == 1


def test_a_thread_he_already_answered_is_not_a_reminder(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    escribe(reader, OWNER, 29, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert slack == []


def test_an_answer_the_database_has_not_seen_yet_still_counts(reader, slack):
    """Lo más importante de todo: la base va por detrás de Slack. Él contestó hace una
    hora y el watcher aún no lo ha traído; avisarle sería decirle que haga lo que ya hizo."""
    raiz = escribe(reader, OWNER, 50)
    respuesta = escribe(reader, "USUNNY", 30, hilo=raiz)
    escribe(reader, OWNER, 1, hilo=raiz, en_base=False)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert slack == []
    # Queda apuntado, así que no se vuelve a preguntar por lo mismo en cada pasada.
    assert apunte(raiz)["resuelto_ts"] == respuesta
    llamadas = len(reader.replies_calls)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert len(reader.replies_calls) == llamadas


def test_an_emoji_reaction_counts_as_an_answer(reader, slack):
    """Igual que en los mensajes directos (0033): contestar con un 👍 es contestar. Las
    reacciones no llegan nunca a la base, así que solo lo sabe la comprobación en vivo."""
    raiz = escribe(reader, OWNER, 50)
    respuesta = escribe(
        reader, "USUNNY", 30, hilo=raiz, reactions=[{"name": "+1", "users": [OWNER]}]
    )
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert apunte(raiz)["resuelto_ts"] == respuesta


def test_someone_elses_reaction_is_not_his_answer(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz, reactions=[{"name": "+1", "users": ["UOTRO"]}])
    assert seguimiento.publicar_avisos(reader=reader) == 1


def test_a_new_reply_after_an_emoji_answer_is_a_new_reminder(reader, slack):
    raiz = escribe(reader, OWNER, 80)
    escribe(reader, "USUNNY", 60, hilo=raiz, reactions=[{"name": "+1", "users": [OWNER]}])
    assert seguimiento.publicar_avisos(reader=reader) == 0
    nueva = escribe(reader, "USUNNY", 30, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert enlace(nueva) in slack[0]["json"]
    assert "more repl" not in slack[0]["json"]


def test_a_deleted_reply_is_nothing_to_answer(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    respuesta = escribe(reader, "USUNNY", 30, hilo=raiz, en_slack=False)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert apunte(raiz)["resuelto_ts"] == respuesta


def test_in_someone_elses_thread_only_the_person_who_started_it_counts(reader, slack):
    """Él contestó en el hilo de Sunny. Que Sunny le conteste es alguien esperándole; que
    un tercero escriba debajo es el resto del club hablando entre ellos."""
    persona()
    raiz = escribe(reader, "USUNNY", 60)
    escribe(reader, OWNER, 50, hilo=raiz)
    escribe(reader, "UTERCERO", 40, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert reader.replies_calls == []
    vuelve = escribe(reader, "USUNNY", 30, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert lineas(slack[0])[-1] == (
        "• *Sunny Patel* — Acme · Sunny replied to you in their thread in #founders-general "
        f"30 hours ago · {enlace(vuelve)}"
    )


def test_a_thread_he_never_wrote_in_is_none_of_his_business(reader, slack):
    raiz = escribe(reader, "USUNNY", 60)
    escribe(reader, "UTERCERO", 30, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 0


def test_without_the_root_of_the_thread_nobody_is_reminded(reader, slack):
    """Sin el principal no se sabe quién abrió el hilo: ante la duda, no avisar."""
    escribe(reader, OWNER, 50, hilo="1700000000.000001")
    escribe(reader, "USUNNY", 30, hilo="1700000000.000001")
    assert seguimiento.publicar_avisos(reader=reader) == 0


def test_bots_discarded_and_test_people_never_count(reader, slack):
    persona("UDIS", "Dis Carded", estado="descartado")
    pid = persona("UTEST", "Test Person")
    db.execute("update prospects set sin_tarjeta = true where id = %s", (pid,))
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "UBOT", 30, hilo=raiz, status="ignorado")
    escribe(reader, "UDIS", 29, hilo=raiz)
    escribe(reader, "UTEST", 28, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert slack == []


def test_the_deadline_runs_from_the_first_unanswered_reply(reader, slack):
    """Un hilo que sigue vivo no aplaza el aviso para siempre, y sale una sola línea."""
    persona()
    raiz = escribe(reader, OWNER, 60)
    primera = escribe(reader, "USUNNY", 30, hilo=raiz)
    escribe(reader, "UBOB", 3, hilo=raiz)
    escribe(reader, "UCLEO", 1, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert lineas(slack[0])[-1] == (
        "• *Sunny Patel* — Acme · Sunny replied in your thread in #founders-general "
        f"30 hours ago (+2 more replies) · {enlace(primera)}"
    )


def test_with_thread_reading_off_there_are_no_thread_reminders_and_no_calls(reader, slack):
    """Apagado degrada sin ruido: las respuestas que quedaron guardadas ya no son de fiar
    y no se le pregunta a Slack por ningún hilo."""
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    ajuste("leer_hilos", False)
    tocado_hace("leer_hilos")
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert seguimiento.publicar_resumen(reader=reader) == 0
    assert slack == []
    assert reader.replies_calls == []
    assert db.fetch_all("select 1 from agent_actions where action = 'seguimiento_fallido'") == []


def test_the_thread_switch_turns_them_off(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    ajuste("seguimiento_hilos_activo", False)
    tocado_hace("seguimiento_hilos_activo")
    assert seguimiento.publicar_avisos(reader=reader) == 0


def test_if_slack_cannot_confirm_it_nobody_is_reminded(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    reader.unavailable_threads.add(raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert slack == []
    assert apunte(raiz) is None
    # En cuanto Slack vuelve a contestar, sale.
    reader.unavailable_threads.clear()
    assert seguimiento.publicar_avisos(reader=reader) == 1


# --- lo viejo no se anuncia --------------------------------------------------------


def test_nothing_from_before_the_switch_was_last_touched_is_announced(reader, slack):
    """El primer despliegue (o volver a encenderlo) no vacía en el canal lo que llevaba
    días sin contestar: es la inundación que ya pasó con los DMs y con las presentaciones."""
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    menciona(reader, "UBOB", 30)
    tocado_hace("seguimiento_hilos_activo", dias=0)
    tocado_hace("seguimiento_menciones_activo", dias=0)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert seguimiento.publicar_resumen(reader=reader) == 0
    assert reader.replies_calls == []


def test_turning_thread_reading_on_does_not_announce_the_backlog_it_brings(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    tocado_hace("leer_hilos", dias=0)
    assert seguimiento.publicar_avisos(reader=reader) == 0


def test_a_freshly_migrated_database_announces_nothing_old(conn, restore_config, slack):
    """Sin tocar nada: los interruptores nacen con la fecha de la migración."""
    ajuste("leer_hilos", True)
    lector = FakeReader(owner=OWNER)
    raiz = escribe(lector, OWNER, 50)
    escribe(lector, "USUNNY", 30, hilo=raiz)
    menciona(lector, "UBOB", 30)
    assert seguimiento.publicar_avisos(reader=lector) == 0
    assert slack == []


def test_after_a_week_it_stops_being_brought_up(reader, slack):
    raiz = escribe(reader, OWNER, 24 * 10)
    escribe(reader, "USUNNY", 24 * 8, hilo=raiz)
    menciona(reader, "UBOB", 24 * 8)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert seguimiento.publicar_resumen(reader=reader) == 0
    ajuste("seguimiento_caduca_dias", 9)
    assert seguimiento.publicar_avisos(reader=reader) == 2


# --- repetirse, y dejar de hacerlo --------------------------------------------------


def test_it_repeats_after_the_reminder_window_and_stops_at_the_cap(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    # Dentro de la ventana no se repite.
    assert seguimiento.publicar_avisos(reader=reader) == 0
    pasan_horas(5)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    pasan_horas(5)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    # Tres avisos (seguimiento_canal_max_avisos): a partir de aquí ya no interrumpe...
    pasan_horas(5)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert len(slack) == 3
    assert apunte(raiz)["avisos"] == 3
    # ...pero sigue en la foto del día mientras siga pendiente.
    assert seguimiento.publicar_resumen(reader=reader) == 1
    assert "*Thread replies you haven't answered*" in lineas(slack[3])
    assert lineas(slack[3])[0] == "🟡 *TODAY* · ⏰ *FOLLOW-UPS*"


def test_with_a_cap_of_zero_it_never_interrupts(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    ajuste("seguimiento_canal_max_avisos", 0)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert seguimiento.publicar_resumen(reader=reader) == 1


def test_once_he_answers_the_reminders_stop(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    escribe(reader, OWNER, 0.1, hilo=raiz, en_base=False)
    pasan_horas(5)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert seguimiento.publicar_resumen(reader=reader) == 0
    assert len(slack) == 1


def test_a_reply_after_his_answer_starts_the_count_again(reader, slack):
    raiz = escribe(reader, OWNER, 24 * 5)
    escribe(reader, "USUNNY", 24 * 4, hilo=raiz)
    for _ in range(3):
        assert seguimiento.publicar_avisos(reader=reader) == 1
        pasan_horas(5)
    escribe(reader, OWNER, 24 * 3, hilo=raiz)
    nueva = escribe(reader, "USUNNY", 30, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    fila = apunte(raiz)
    assert (fila["pendiente_ts"], fila["avisos"]) == (nueva, 1)


def test_a_failed_post_gives_the_reminder_back(reader, slack, monkeypatch):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)

    def boom(blocks, text):
        raise RuntimeError("slack caído")

    with monkeypatch.context() as m:
        m.setattr(seguimiento.slack_writer, "post_card", boom)
        assert seguimiento.publicar_avisos(reader=reader) == 0
    fila = apunte(raiz)
    assert (fila["avisos"], fila["avisado_at"]) == (0, None)
    # No gastó ninguno de los suyos: sale en la pasada siguiente.
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert apunte(raiz)["avisos"] == 1


def test_the_reservation_is_spent_before_posting_and_only_once(reader, slack):
    """Dos workers a la vez, o un reintento: solo uno se lleva el aviso."""
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    ahora = seguimiento.datetime.now(seguimiento.UTC)
    (pendiente,) = seguimiento_canal.candidatos(OWNER, ahora)
    assert seguimiento_canal.reservar(pendiente, ahora, 4) is True
    assert seguimiento_canal.reservar(pendiente, ahora, 4) is False
    assert apunte(raiz)["avisos"] == 1
    # Y lo ya reservado deja de ser candidato para el aviso inmediato.
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert slack == []


# --- menciones ----------------------------------------------------------------------


def test_an_unanswered_mention_is_a_reminder(reader, slack):
    persona()
    marca = menciona(reader, "USUNNY", 5)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert lineas(slack[0]) == [
        "🟡 *TODAY* · ⏳ *WAITING ON YOU*",
        "",
        "*Mentions you haven't answered*",
        (
            "• *Sunny Patel* — Acme · Sunny mentioned you in #founders-general "
            f"5 hours ago · {enlace(marca)}"
        ),
    ]
    assert apunte(marca)["tipo"] == "mencion"


def test_a_recent_mention_waits_for_its_deadline(reader, slack):
    menciona(reader, "USUNNY", 2)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    ajuste("seguimiento_mencion_horas", 1)
    assert seguimiento.publicar_avisos(reader=reader) == 1


def test_a_mention_he_answered_in_its_thread_is_not_a_reminder(reader, slack):
    marca = menciona(reader, "USUNNY", 5)
    escribe(reader, OWNER, 4, hilo=marca)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert reader.replies_calls == []


def test_a_thread_answer_only_slack_knows_about_still_counts(reader, slack):
    marca = menciona(reader, "USUNNY", 5)
    escribe(reader, OWNER, 1, hilo=marca, en_base=False)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert apunte(marca)["resuelto_ts"] == marca


def test_writing_in_the_channel_afterwards_answers_a_loose_mention(reader, slack):
    """Generoso a propósito: un mensaje suyo en ese canal después de la mención la da
    por contestada, esté en la base o solo en Slack."""
    en_base = menciona(reader, "USUNNY", 6)
    escribe(reader, OWNER, 5.5)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert apunte(en_base) is None  # ni hizo falta preguntarle a Slack

    db.execute("truncate slack_messages, eventos_canal, seguimiento_canal")
    reader.messages.clear()
    solo_slack = menciona(reader, "USUNNY", 6)
    escribe(reader, OWNER, 1, en_base=False)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert apunte(solo_slack)["resuelto_ts"] == solo_slack
    assert slack == []


def test_what_he_wrote_before_the_mention_or_elsewhere_does_not_answer_it(reader, slack):
    otro_hilo = escribe(reader, "UBOB", 40)
    menciona(reader, "USUNNY", 6)
    escribe(reader, OWNER, 10)  # antes de la mención
    escribe(reader, OWNER, 2, hilo=otro_hilo)  # después, pero en otro hilo
    escribe(reader, OWNER, 1, canal="COTRO")  # después, pero en otro canal
    assert seguimiento.publicar_avisos(reader=reader) == 1


def test_reacting_to_a_mention_answers_it(reader, slack):
    marca = menciona(reader, "USUNNY", 5, reactions=[{"name": "eyes", "users": [OWNER]}])
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert apunte(marca)["resuelto_ts"] == marca


def test_a_mention_inside_a_thread_waits_for_an_answer_in_that_thread(reader, slack):
    persona()
    raiz = escribe(reader, "UBOB", 40)
    marca = menciona(reader, "USUNNY", 6, hilo=raiz)
    # Un mensaje suyo en el canal no contesta una mención hecha dentro de un hilo.
    escribe(reader, OWNER, 3)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert lineas(slack[0])[-1] == (
        "• *Sunny Patel* — Acme · Sunny mentioned you in #founders-general (in a thread) "
        f"6 hours ago · {enlace(marca)}"
    )
    # La fila es la del hilo, y lo pendiente es la mención.
    assert apunte(raiz)["pendiente_ts"] == marca
    escribe(reader, OWNER, 0.1, hilo=raiz, en_base=False)
    pasan_horas(5)
    assert seguimiento.publicar_avisos(reader=reader) == 0


def test_a_thread_with_a_mention_and_replies_is_one_line(reader, slack):
    raiz = escribe(reader, OWNER, 60)
    escribe(reader, "UBOB", 40, hilo=raiz)
    menciona(reader, "USUNNY", 30, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    texto = slack[0]["blocks"][0]["text"]["text"]
    assert texto.count("•") == 1
    assert "*Mentions you haven't answered*" in texto
    assert "Thread replies" not in texto


def test_skipped_mentions_and_discarded_people_are_not_reminded(reader, slack):
    persona("UDIS", "Dis Carded", estado="descartado")
    menciona(reader, "UDIS", 5)
    menciona(reader, "UBOB", 6, estado="omitida")
    assert seguimiento.publicar_avisos(reader=reader) == 0


def test_the_mention_switch_turns_them_off(reader, slack):
    menciona(reader, "USUNNY", 5)
    ajuste("seguimiento_menciones_activo", False)
    tocado_hace("seguimiento_menciones_activo")
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert seguimiento.publicar_resumen(reader=reader) == 0


# --- menciones con la lectura de hilos apagada ---------------------------------------


@pytest.fixture
def sin_hilos(reader):
    ajuste("leer_hilos", False)
    tocado_hace("leer_hilos")
    return reader


def test_with_threads_off_a_loose_mention_is_still_a_reminder(sin_hilos, slack):
    marca = menciona(sin_hilos, "USUNNY", 5)
    assert seguimiento.publicar_avisos(reader=sin_hilos) == 1
    assert enlace(marca) in slack[0]["json"]
    # Se comprobó con el propio mensaje, sin abrir ningún hilo.
    assert sin_hilos.mensaje_calls == [(CANAL, marca)]
    assert sin_hilos.replies_calls == []


def test_with_threads_off_his_thread_answer_shows_in_the_message_itself(sin_hilos, slack):
    marca = menciona(sin_hilos, "USUNNY", 5, reply_users=["UBOB", OWNER], reply_users_count=2)
    assert seguimiento.publicar_avisos(reader=sin_hilos) == 0
    assert apunte(marca)["resuelto_ts"] == marca
    assert sin_hilos.replies_calls == []


def test_with_threads_off_a_reaction_still_answers(sin_hilos, slack):
    menciona(sin_hilos, "USUNNY", 5, reactions=[{"name": "+1", "users": [OWNER]}])
    assert seguimiento.publicar_avisos(reader=sin_hilos) == 0


def test_with_threads_off_a_truncated_list_of_repliers_is_doubt_and_doubt_is_silence(
    sin_hilos, slack
):
    """Slack recorta `reply_users`: si contestaron más de los que vienen, él puede estar
    entre los que faltan. No se sabe, así que no se avisa -- y tampoco se da por resuelto."""
    marca = menciona(
        sin_hilos, "USUNNY", 5, reply_users=["UA", "UB", "UC", "UD", "UE"], reply_users_count=8
    )
    assert seguimiento.publicar_avisos(reader=sin_hilos) == 0
    assert apunte(marca) is None
    assert slack == []


def test_with_threads_off_a_mention_inside_a_thread_is_left_alone(sin_hilos, slack):
    raiz = escribe(sin_hilos, "UBOB", 40)
    menciona(sin_hilos, "USUNNY", 6, hilo=raiz)
    assert seguimiento.publicar_avisos(reader=sin_hilos) == 0
    assert sin_hilos.replies_calls == [] and sin_hilos.mensaje_calls == []


def test_with_threads_off_a_slack_failure_is_silence(sin_hilos, slack):
    menciona(sin_hilos, "USUNNY", 5)
    sin_hilos.unavailable_channels.add(CANAL)
    assert seguimiento.publicar_avisos(reader=sin_hilos) == 0
    assert slack == []


# --- quién, dónde, y nunca qué -------------------------------------------------------


def test_someone_without_a_file_gets_their_name_from_slack(reader, slack):
    reader.add_profile("UNEW", "Nora New")
    menciona(reader, "UNEW", 5)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert "• *Nora New* · Nora mentioned you in #founders-general 5 hours ago" in slack[0]["json"]
    # El nombre no se guarda: quien no es un lead sigue sin ficha.
    assert db.fetch_all("select 1 from prospects") == []


def test_without_a_name_the_line_says_someone_never_a_slack_code(reader, slack):
    menciona(reader, "UNADIE", 5)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert "• *Someone* · Someone mentioned you" in slack[0]["json"]
    assert "UNADIE" not in slack[0]["json"]


def test_a_channel_without_a_name_shows_its_code(reader, slack):
    db.execute("delete from canales")
    menciona(reader, "USUNNY", 5)
    seguimiento.publicar_avisos(reader=reader)
    assert f"mentioned you in {CANAL} 5 hours ago" in slack[0]["json"]


def test_names_from_slack_cannot_break_the_block_or_ping_anyone(reader, slack):
    persona("UX", "<!channel> *Ada*", "Acme <https://evil|click>")
    db.execute("update canales set nombre = 'general <!here>'")
    menciona(reader, "UX", 5)
    seguimiento.publicar_avisos(reader=reader)
    texto = slack[0]["blocks"][0]["text"]["text"]
    assert "<!channel>" not in texto and "<!here>" not in texto
    assert "&lt;!here&gt;" in texto
    assert "<https://evil" not in texto
    # El único enlace vivo es el del aviso.
    assert texto.count("|Open in Slack>") == 1 and texto.count("<http") == 1


def test_a_link_slack_will_not_give_leaves_the_line_without_one(reader, slack, monkeypatch):
    menciona(reader, "USUNNY", 5)
    monkeypatch.setattr(reader, "permalink", lambda canal, marca: None)
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert "Open in Slack" not in slack[0]["json"]


def test_a_reminder_never_quotes_what_was_said(reader, slack):
    raiz = escribe(reader, OWNER, 60)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    menciona(reader, "UBOB", 5)
    assert seguimiento.publicar_avisos(reader=reader) == 2
    assert seguimiento.publicar_resumen(reader=reader) == 2
    for publicado in slack:
        assert SECRETO not in publicado["json"] and "ingenieros" not in publicado["json"]
        assert SECRETO not in publicado["text"]


def test_the_module_never_reads_the_text_of_a_message():
    """Todo es por reglas sobre quién, dónde y cuándo: ni el texto que hay en la base ni
    el que devuelve Slack entran aquí, así que tampoco pueden salir (ni llegar a un modelo)."""
    codigo = Path("src/handoff_agent/delivery/seguimiento_canal.py").read_text()
    arbol = ast.parse(codigo)
    claves = {n.value for n in ast.walk(arbol) if isinstance(n, ast.Constant) and n.value == "text"}
    assert claves == set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            assert ".text" not in nodo.value.replace("::text", "")
    assert "llm" not in codigo and "untrusted" not in codigo


# --- mensajes directos de gente sin ficha --------------------------------------------


@pytest.fixture
def sin_ficha(reader):
    ajuste("seguimiento_sin_ficha_activo", True)
    tocado_hace("seguimiento_sin_ficha_activo")
    reader.add_profile("UDANA", "Dana Okafor")
    return reader


def test_people_without_a_file_stay_out_unless_it_is_switched_on(reader, slack):
    reader.add_profile("UDANA", "Dana Okafor")
    dm_sin_ficha()
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert seguimiento.publicar_resumen(reader=reader) == 0
    assert slack == []


def test_switched_on_a_non_lead_who_replied_is_a_reminder(sin_ficha, slack):
    dm_sin_ficha()
    assert seguimiento.publicar_avisos(reader=sin_ficha) == 1
    assert lineas(slack[0]) == [
        "🟡 *TODAY* · ⏳ *WAITING ON YOU*",
        "",
        "*Direct messages from people who aren't leads yet*",
        (
            "• *Dana Okafor* · Dana replied 1 day ago · "
            "<https://app.slack.com/client/T0OWNER/DZZZ|Open in Slack>"
        ),
    ]


def test_a_non_lead_interrupts_once_and_then_stays_in_the_daily_summary(sin_ficha, slack):
    dm_sin_ficha()
    assert seguimiento.publicar_avisos(reader=sin_ficha) == 1
    # Ni pasada la ventana de recordar: a un lead se le insiste, a esta gente no.
    db.execute("update dm_rastro set avisado_at = now() - interval '2 days'")
    assert seguimiento.publicar_avisos(reader=sin_ficha) == 0
    assert seguimiento.publicar_resumen(reader=sin_ficha) == 1
    assert "*Direct messages from people who aren't leads yet*" in lineas(slack[1])


def test_only_what_arrived_after_switching_it_on_counts(sin_ficha, slack):
    """Encenderlo no saca de golpe todas las conversaciones que ya estaban esperando."""
    dm_sin_ficha()
    tocado_hace("seguimiento_sin_ficha_activo", dias=0)
    assert seguimiento.publicar_avisos(reader=sin_ficha) == 0
    assert seguimiento.publicar_resumen(reader=sin_ficha) == 0


def test_a_non_lead_without_a_name_in_slack_is_not_announced(sin_ficha, slack):
    """El ruido que ya se quitó una vez: "U0BCQGYDU64 replied 83 days ago"."""
    dm_sin_ficha("DSIN", "UNADIE")
    sin_ficha.add_profile("UBOT", "Deploy Bot", is_bot=True)
    dm_sin_ficha("DBOT", "UBOT")
    assert seguimiento.publicar_avisos(reader=sin_ficha) == 0
    assert slack == []


def test_non_lead_cases_respect_the_deadline_the_expiry_and_the_emoji(sin_ficha, slack):
    dm_sin_ficha("DPRONTO", "UDANA", horas=5)  # aún dentro de las 24 horas
    sin_ficha.add_profile("UVIEJO", "Vie Jo")
    dm_sin_ficha("DVIEJO", "UVIEJO", horas=24 * 9)  # caducado
    sin_ficha.add_profile("UEMOJI", "Emo Ji")
    dm_sin_ficha("DEMOJI", "UEMOJI")
    db.execute("update dm_rastro set respondido_con_emoji = true where dm_id = 'DEMOJI'")
    sin_ficha.add_profile("UYO", "Yo Mismo")
    dm_sin_ficha("DYO", "UYO", de="dueno")  # escribió él: no depende de él
    assert seguimiento.publicar_avisos(reader=sin_ficha) == 0


def test_someone_with_a_file_never_goes_down_the_non_lead_path(sin_ficha, slack):
    """El rastro enlaza la ficha en su pasada siguiente; entre tanto, un descartado con
    la fila aún sin enlazar no puede colarse como "gente sin ficha"."""
    persona("UDANA", "Dana Okafor", estado="descartado")
    dm_sin_ficha()
    assert seguimiento.publicar_avisos(reader=sin_ficha) == 0


def test_nothing_is_ever_read_or_suggested_for_a_non_lead(sin_ficha, slack, monkeypatch):
    pedidas = []
    monkeypatch.setattr(sugerencia, "sugerir", lambda dm_id: pedidas.append(dm_id))
    dm_sin_ficha()
    dm_de_lead()
    assert seguimiento.publicar_avisos(reader=sin_ficha) == 2
    assert pedidas == ["DAAA"]
    assert sin_ficha.last_message_calls == [] and sin_ficha.history_calls == []


# --- con lo que ya había ---------------------------------------------------------------


def test_without_a_reader_everything_is_exactly_as_before(reader, slack):
    """El MCP y cualquiera que llame sin lector: solo los mensajes directos con leads."""
    ajuste("seguimiento_sin_ficha_activo", True)
    tocado_hace("seguimiento_sin_ficha_activo")
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    menciona(reader, "UBOB", 5)
    dm_sin_ficha()
    dm_de_lead()
    assert [c.tipo for c in seguimiento.casos()] == [seguimiento.RESPONDER]
    assert seguimiento.publicar_avisos() == 1
    assert "Lee Lead" in slack[0]["json"]
    assert "Thread replies" not in slack[0]["json"] and "Mentions" not in slack[0]["json"]


def test_everything_shares_one_message_with_a_subtitle_each(sin_ficha, slack):
    raiz = escribe(sin_ficha, OWNER, 50)
    escribe(sin_ficha, "USUNNY", 30, hilo=raiz)
    menciona(sin_ficha, "UBOB", 5)
    dm_sin_ficha()
    dm_de_lead()
    assert seguimiento.publicar_avisos(reader=sin_ficha) == 4
    (aviso,) = slack
    titulos = [linea for linea in lineas(aviso) if linea.startswith("*")]
    assert titulos == [
        "*They replied, you haven't*",
        "*Mentions you haven't answered*",
        "*Thread replies you haven't answered*",
        "*Direct messages from people who aren't leads yet*",
    ]
    assert aviso["text"] == "🟡 Today · Follow-ups: 4 conversations waiting"
    (accion,) = db.fetch_all("select payload from agent_actions where action = 'seguimiento_aviso'")
    assert accion["payload"]["tipos"] == ["responder", "mencion", "hilo", "responder_sin_ficha"]


def test_the_daily_summary_carries_them_too(sin_ficha, slack):
    raiz = escribe(sin_ficha, OWNER, 50)
    escribe(sin_ficha, "USUNNY", 30, hilo=raiz)
    menciona(sin_ficha, "UBOB", 5)
    dm_sin_ficha()
    assert seguimiento.publicar_resumen(reader=sin_ficha) == 3
    assert lineas(slack[0])[0] == "🟡 *TODAY* · ⏰ *FOLLOW-UPS*"
    assert [linea for linea in lineas(slack[0]) if linea.startswith("*")] == [
        "*Mentions you haven't answered*",
        "*Thread replies you haven't answered*",
        "*Direct messages from people who aren't leads yet*",
    ]
    # El resumen no gasta los avisos inmediatos de un hilo.
    assert apunte(raiz) is None


def test_the_daily_summary_checks_slack_before_listing_them(reader, slack):
    raiz = escribe(reader, OWNER, 50)
    escribe(reader, "USUNNY", 30, hilo=raiz)
    escribe(reader, OWNER, 1, hilo=raiz, en_base=False)
    assert seguimiento.publicar_resumen(reader=reader) == 0
    assert slack == []


def test_they_share_the_daily_cap_and_leads_go_first(reader, slack):
    ajuste("seguimiento_avisos_por_dia", 1)
    menciona(reader, "UBOB", 5)
    dm_de_lead()
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert "Lee Lead" in slack[0]["json"] and "mentioned you" not in slack[0]["json"]
    # El cupo del día está gastado: la mención espera al resumen, sin haber gastado nada.
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert db.fetch_all("select 1 from seguimiento_canal") == []


def test_the_summary_cap_counts_them(reader, slack):
    ajuste("seguimiento_max_por_resumen", 2)
    for i, usuario in enumerate(("UA", "UB", "UC")):
        menciona(reader, usuario, 10 + i)
    assert seguimiento.publicar_resumen(reader=reader) == 2


def test_the_switches_of_the_whole_follow_up_still_rule(reader, slack):
    menciona(reader, "USUNNY", 5)
    ajuste("seguimiento_activo", False)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    ajuste("seguimiento_activo", True)
    ajuste("kill_switch", True)
    assert seguimiento.publicar_avisos(reader=reader) == 0
    assert slack == [] and reader.replies_calls == []


def test_a_dead_credential_does_not_take_the_lead_reminders_with_it(reader, slack):
    menciona(reader, "USUNNY", 5)
    dm_de_lead()
    reader.auth_error = SlackAuthFailed("invalid_auth")
    assert seguimiento.publicar_avisos(reader=reader) == 1
    assert "Lee Lead" in slack[0]["json"]


def test_a_failure_checking_one_case_does_not_lose_the_others(reader, slack):
    rota = menciona(reader, "UBOB", 6)
    menciona(reader, "USUNNY", 5)
    original = reader.respuestas

    def respuestas(canal, hilo, oldest=None):
        if hilo == rota:
            raise RuntimeError("respuesta rara de Slack")
        return original(canal, hilo, oldest)

    reader.respuestas = respuestas
    assert seguimiento.publicar_avisos(reader=reader) == 1


def test_old_bookkeeping_is_cleaned_up(reader):
    db.execute(
        "insert into seguimiento_canal (channel_id, clave_ts, tipo, pendiente_ts, actualizado_at) "
        "values ('C1', '1.1', 'hilo', '1.2', now() - interval '100 days'), "
        "       ('C1', '2.1', 'hilo', '2.2', now())"
    )
    assert seguimiento_canal.limpiar() == 1
    assert [f["clave_ts"] for f in db.fetch_all("select clave_ts from seguimiento_canal")] == [
        "2.1"
    ]


# --- la migración ------------------------------------------------------------------------


def _config(clave):
    return db.fetch_one("select value from config where key = %s", (clave,))["value"]


def test_the_new_settings_ship_with_their_defaults(conn):
    assert _config("seguimiento_hilos_activo") is True
    assert _config("seguimiento_menciones_activo") is True
    # Enseñar con quién habla AG fuera de sus leads lo decide él: nace apagado.
    assert _config("seguimiento_sin_ficha_activo") is False
    assert _config("seguimiento_hilo_horas") == seguimiento_canal.HORAS_HILO == 24
    assert _config("seguimiento_mencion_horas") == seguimiento_canal.HORAS_MENCION == 4
    assert _config("seguimiento_canal_max_avisos") == seguimiento_canal.MAX_AVISOS == 3
    assert _config("seguimiento_caduca_dias") == seguimiento_canal.CADUCA_DIAS == 7


@pytest.fixture
def equipo(conn, restore_config):
    db.execute("insert into panel_users (email) values (%s)", (ALLOWED,))


def test_the_panel_can_tune_them_and_a_stranger_cannot(equipo):
    as_user(ALLOWED, "update config set value = 'true' where key = 'seguimiento_sin_ficha_activo'")
    as_user(ALLOWED, "update config set value = '12' where key = 'seguimiento_mencion_horas'")
    assert _config("seguimiento_sin_ficha_activo") is True
    assert _config("seguimiento_mencion_horas") == 12
    as_user(STRANGER, "update config set value = '99' where key = 'seguimiento_mencion_horas'")
    as_user(STRANGER, "update config set value = 'false' where key = 'seguimiento_hilos_activo'")
    assert _config("seguimiento_mencion_horas") == 12
    assert _config("seguimiento_hilos_activo") is True


@pytest.mark.parametrize(
    ("clave", "valor"),
    [
        ("seguimiento_hilos_activo", "1"),
        ("seguimiento_menciones_activo", '"true"'),
        ("seguimiento_sin_ficha_activo", "null"),
        ("seguimiento_hilo_horas", "true"),
        ("seguimiento_mencion_horas", "-1"),
        ("seguimiento_canal_max_avisos", '"3"'),
        ("seguimiento_caduca_dias", "{}"),
    ],
)
def test_each_setting_only_takes_its_own_kind_of_value(equipo, clave, valor):
    antes = _config(clave)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_user(ALLOWED, "update config set value = %s::jsonb where key = %s", (valor, clave))
    assert _config(clave) == antes


def test_the_bookkeeping_table_is_closed_to_the_panel(equipo):
    """RLS activado y sin permisos: contabilidad interna del worker, como `hilos`."""
    fila = db.fetch_one(
        "select relrowsecurity from pg_class where oid = 'public.seguimiento_canal'::regclass"
    )
    assert fila["relrowsecurity"] is True
    for sql in (
        "select * from seguimiento_canal",
        "delete from seguimiento_canal",
        (
            "insert into seguimiento_canal (channel_id, clave_ts, tipo, pendiente_ts) "
            "values ('C1', '1.1', 'hilo', '1.2')"
        ),
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_user(ALLOWED, sql)
