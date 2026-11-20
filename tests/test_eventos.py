"""Los dos avisos de canal que no cuestan una llamada al modelo (0030).

Lo que cuidan estos tests:

1. Que detecten lo que tienen que detectar y nada más: una mención es su id en el texto,
   una presentación es el canal configurado, y sin canal configurado no se avisa de
   ninguna presentación.
2. Que no cuesten nada: ni una llamada al modelo, ni un apunte de coste.
3. Que lo que escribió un tercero no pueda hacer daño en el aviso -- una URL de phishing
   en un mensaje no se convierte en enlace.
"""

import json
import time
from types import SimpleNamespace

import pytest

from handoff_agent import db, llm
from handoff_agent.signals import eventos
from tests.slack_fakes import FakeReader

DUENO = "UANTHONY"
CANAL = "C0INTROS"
OTRO_CANAL = "C0GENERAL"


@pytest.fixture(autouse=True)
def slack(monkeypatch):
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    posted = []

    def post_card(blocks, text):
        posted.append({"json": json.dumps(blocks, ensure_ascii=False), "text": text})
        return "CHANDOFF", f"1700000000.{len(posted):06d}"

    monkeypatch.setattr(eventos.slack_writer, "post_card", post_card)
    return posted


SALUDO = "Welcome Mike! Running logistics for 40 trucks sounds intense. How's it going?"


@pytest.fixture(autouse=True)
def modelo(monkeypatch):
    """El saludo sugerido (0037) es lo único de aquí que llama al modelo: siempre
    sustituido. `respuesta` es lo que devuelve; `llamadas`, cada prompt que recibió."""
    estado = SimpleNamespace(respuesta={"saludo": SALUDO}, llamadas=[])

    def complete(prompt, **kwargs):
        estado.llamadas.append({"prompt": prompt, **kwargs})
        r = estado.respuesta
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(text=r if isinstance(r, str) else json.dumps(r))

    monkeypatch.setattr(llm, "complete", complete)
    return estado


class FakeLector:
    def owner_id(self):
        return DUENO

    def permalink(self, channel, ts):
        return f"https://fake.slack.com/archives/{channel}/p{ts.replace('.', '')}"


@pytest.fixture
def lector():
    return FakeLector()


def mensaje(texto, canal=OTRO_CANAL, user="UMIKE", ts=None):
    ts = ts or f"{time.time():.6f}"
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) values (%s, %s, %s, %s)",
        (canal, ts, user, texto),
    )
    return ts


def persona(slack_user_id="UMIKE", nombre="Mike Black", estado="nuevo"):
    return db.fetch_one(
        "insert into prospects (slack_user_id, full_name, company_name, state) "
        "values (%s, %s, 'Acme', %s) returning id",
        (slack_user_id, nombre, estado),
    )["id"]


def con_canal_de_intros(conn):
    db.execute(
        "update config set value = %s::jsonb where key = 'canal_presentaciones'",
        (json.dumps(CANAL),),
    )


def filas(tipo=None):
    if tipo:
        return db.fetch_all("select * from eventos_canal where tipo = %s", (tipo,))
    return db.fetch_all("select * from eventos_canal")


# ---------------------------------------------------------------------------------
# Menciones
# ---------------------------------------------------------------------------------


def test_a_mention_is_detected(conn, lector, slack):
    persona()
    mensaje(f"hey <@{DUENO}> can you intro me to someone?")
    assert eventos.publicar_pendientes(lector) == 1
    assert "YOU WERE MENTIONED" in slack[0]["json"]
    assert "Mike Black" in slack[0]["json"]


def test_a_message_without_the_mention_is_not_an_event(conn, lector, slack):
    mensaje("hola a todos, buenos días")
    assert eventos.publicar_pendientes(lector) == 0
    assert filas() == []


def test_mentioning_someone_else_is_not_an_event(conn, lector, slack):
    mensaje("hey <@UOTRAPERSONA> mira esto")
    assert eventos.publicar_pendientes(lector) == 0


def test_the_owner_mentioning_himself_is_not_an_event(conn, lector, slack):
    """Sus propios mensajes no avisan: ni mencionarse ni presentarse en su club."""
    mensaje(f"recordatorio para mí <@{DUENO}>", user=DUENO)
    assert eventos.publicar_pendientes(lector) == 0


def test_an_ignored_message_is_not_an_event(conn, lector, slack):
    ts = mensaje(f"<@{DUENO}> mira", user="UBOT")
    db.execute("update slack_messages set status = 'ignorado' where ts = %s", (ts,))
    assert eventos.publicar_pendientes(lector) == 0


def test_a_discarded_person_never_triggers_an_event(conn, lector, slack):
    persona(estado="descartado")
    mensaje(f"<@{DUENO}> hola")
    assert eventos.publicar_pendientes(lector) == 0


def test_a_mention_from_someone_unknown_still_alerts(conn, lector, slack):
    """Quien menciona a AG puede no estar investigado todavía: se avisa igual."""
    mensaje(f"<@{DUENO}> hi there", user="UNUEVO")
    assert eventos.publicar_pendientes(lector) == 1
    assert "UNUEVO" in slack[0]["json"]


# ---------------------------------------------------------------------------------
# Presentaciones
# ---------------------------------------------------------------------------------


def test_without_a_configured_channel_there_are_no_introductions(conn, lector, slack):
    """No se adivina por el nombre: sin canal configurado, no se avisa de nada."""
    mensaje("Hi everyone, I'm new here!", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 0


def test_a_message_in_the_introductions_channel_is_an_introduction(
    conn, lector, slack, restore_config
):
    con_canal_de_intros(conn)
    persona()
    mensaje("Hi everyone, I run a logistics company", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 1
    assert "NEW INTRODUCTION" in slack[0]["json"]


def test_a_message_in_another_channel_is_not_an_introduction(conn, lector, slack, restore_config):
    con_canal_de_intros(conn)
    mensaje("Hi everyone", canal=OTRO_CANAL)
    assert eventos.publicar_pendientes(lector) == 0


def test_an_empty_message_is_not_an_introduction(conn, lector, slack, restore_config):
    con_canal_de_intros(conn)
    mensaje("   ", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 0


def test_one_message_can_be_both_things(conn, lector, slack, restore_config):
    """Alguien que se presenta y menciona a AG en la misma línea: dos avisos."""
    con_canal_de_intros(conn)
    persona()
    mensaje(f"Hi all, I'm Mike. <@{DUENO}> told me to join", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 2
    assert {f["tipo"] for f in filas()} == {eventos.MENCION, eventos.PRESENTACION}


# ---------------------------------------------------------------------------------
# No repetirse, topes y frenos
# ---------------------------------------------------------------------------------


def test_an_event_is_only_published_once(conn, lector, slack):
    persona()
    mensaje(f"<@{DUENO}> hola")
    assert eventos.publicar_pendientes(lector) == 1
    assert eventos.publicar_pendientes(lector) == 0
    assert len(slack) == 1


def test_the_daily_cap_applies_per_type(conn, lector, slack, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'menciones_por_dia'")
    mensaje(f"<@{DUENO}> uno", user="U1", ts=f"{time.time() - 20:.6f}")
    mensaje(f"<@{DUENO}> dos", user="U2", ts=f"{time.time() - 10:.6f}")
    assert eventos.publicar_pendientes(lector) == 1
    # La segunda sigue pendiente: mañana sale.
    assert len([f for f in filas(eventos.MENCION) if f["estado"] == "pendiente"]) == 1


def test_a_failed_post_leaves_the_event_pending(conn, lector, slack, monkeypatch):
    persona()
    mensaje(f"<@{DUENO}> hola")

    def boom(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(eventos.slack_writer, "post_card", boom)
    assert eventos.publicar_pendientes(lector) == 0
    assert filas(eventos.MENCION)[0]["estado"] == "pendiente"
    # Y en la vuelta siguiente, con Slack de vuelta, sale.
    monkeypatch.undo()


def test_without_the_bot_configured_nothing_is_published(conn, lector, slack, monkeypatch):
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN", raising=False)
    mensaje(f"<@{DUENO}> hola")
    assert eventos.publicar_pendientes(lector) == 0


def test_publishing_never_raises(conn, monkeypatch, slack):
    class RotoLector:
        def owner_id(self):
            raise RuntimeError("credencial muerta")

    assert eventos.publicar_pendientes(RotoLector()) == 0


# ---------------------------------------------------------------------------------
# Lo que escribió un tercero no puede hacer daño
# ---------------------------------------------------------------------------------


def test_a_url_in_the_message_is_not_turned_into_a_link(conn, lector, slack):
    """Una URL de phishing en el mensaje de alguien no puede salir clicable."""
    mensaje(f"<@{DUENO}> verify here: https://evil.co/verify")
    eventos.publicar_pendientes(lector)
    bloque = slack[0]["json"]
    # La cita va dentro de un código en línea: Slack no autoenlaza ahí.
    assert "`" in bloque
    assert "<https://evil.co/verify|" not in bloque


def test_an_event_costs_nothing(conn, lector, slack):
    """Ni una llamada al modelo: es la razón de que esto viva fuera de las señales."""
    persona()
    mensaje(f"<@{DUENO}> hola")
    eventos.publicar_pendientes(lector)
    assert db.fetch_one("select count(*) as n from llm_calls")["n"] == 0
    assert db.fetch_one("select count(*) as n from cost_events")["n"] == 0


# ---------------------------------------------------------------------------------
# Nada de histórico: el fallo que esto evita costó 40 mensajes en el canal.
# ---------------------------------------------------------------------------------


def test_an_old_message_is_never_announced(conn, lector, slack, restore_config):
    """`slack_messages` lleva meses de histórico de los canales vigilados. Sin el corte
    de edad, la primera pasada anuncia como recién llegadas decenas de presentaciones y
    menciones de hace semanas -- que es exactamente lo que pasó la primera vez."""
    con_canal_de_intros(conn)
    viejo = f"{time.time() - 40 * 86400:.6f}"
    mensaje("Hi everyone, I'm new!", canal=CANAL, ts=viejo)
    mensaje(f"<@{DUENO}> mira esto", ts=viejo)
    assert eventos.publicar_pendientes(lector) == 0
    assert filas() == []


def test_the_whole_history_of_the_introductions_channel_stays_quiet(
    conn, lector, slack, restore_config
):
    con_canal_de_intros(conn)
    for n in range(30):
        mensaje(f"intro {n}", canal=CANAL, user=f"U{n}", ts=f"{time.time() - (n + 2) * 86400:.6f}")
    assert eventos.publicar_pendientes(lector) == 0
    assert slack == []


def test_a_recent_message_still_gets_through(conn, lector, slack, restore_config):
    con_canal_de_intros(conn)
    mensaje("Hi, just joined", canal=CANAL, ts=f"{time.time() - 600:.6f}")
    assert eventos.publicar_pendientes(lector) == 1


# ---------------------------------------------------------------------------------
# Hilos (0032): una respuesta en el canal de presentaciones no es una presentación.
# ---------------------------------------------------------------------------------


def respuesta(texto, thread_ts, canal=CANAL, user="UOTRO"):
    ts = f"{time.time():.6f}"
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, thread_ts) "
        "values (%s, %s, %s, %s, %s)",
        (canal, ts, user, texto, thread_ts),
    )
    return ts


def test_a_welcome_reply_is_not_an_introduction(conn, lector, slack, restore_config):
    """Lo que pasó la primera noche con los hilos: los "Welcome Dajana!" de las
    respuestas salieron como presentaciones nuevas."""
    con_canal_de_intros(conn)
    principal = mensaje("Hi all, I'm Dajana, I run a clinic", canal=CANAL, user="UDAJANA")
    respuesta("Welcome Dajana! Would love to connect", principal)
    respuesta("Sounds like an incredible business! Welcome to FC", principal)

    assert eventos.publicar_pendientes(lector) == 1
    (unica,) = filas(eventos.PRESENTACION)
    assert unica["ts"] == principal


def test_a_mention_inside_a_thread_still_counts(conn, lector, slack):
    """Etiquetar a Anthony dentro de un hilo sí es una mención de verdad."""
    principal = mensaje("Anyone know a good closer agency?", user="UOTRO")
    respuesta(f"<@{DUENO}> you might know someone", principal, canal=OTRO_CANAL, user="UMIKE")
    assert eventos.publicar_pendientes(lector) == 1
    assert filas(eventos.MENCION)


# ---------------------------------------------------------------------------------
# Cada tipo con su aviso (0037): la mención y la presentación ya no comparten plantilla.
# ---------------------------------------------------------------------------------


def bloques(aviso):
    return json.loads(aviso["json"])


def tipos(aviso):
    return [b["type"] for b in bloques(aviso)]


def dossier(prospect_id, contenido):
    db.execute(
        "insert into dossiers (prospect_id, version, content, sources) "
        "values (%s, 1, %s, '[]'::jsonb)",
        (prospect_id, json.dumps(contenido)),
    )


def test_a_mention_and_an_introduction_do_not_look_alike(conn, lector, slack, restore_config):
    """Punto 11 del cliente: cada tipo se reconoce de un vistazo, por su cabecera, por
    su forma y por lo que se lee en la notificación del móvil."""
    con_canal_de_intros(conn)
    persona()
    mensaje(f"hey <@{DUENO}> quick question", ts=f"{time.time() - 20:.6f}")
    mensaje("Hi everyone, I run a logistics company", canal=CANAL, ts=f"{time.time() - 10:.6f}")
    assert eventos.publicar_pendientes(lector) == 2
    mencion, intro = slack

    assert "YOU WERE MENTIONED" in mencion["json"] and "NEW INTRODUCTION" not in mencion["json"]
    assert "NEW INTRODUCTION" in intro["json"] and "YOU WERE MENTIONED" not in intro["json"]
    assert tipos(mencion) == ["section", "context"]
    assert tipos(intro) == ["section", "section", "section", "section", "actions"]
    assert mencion["text"].startswith("Mention: Mike Black tagged you")
    assert intro["text"] == "New introduction: Mike Black — say hi"


def test_a_mention_stays_short_and_free(conn, lector, slack, modelo):
    """La mención no pide saludo ni LinkedIn: solo ir a contestar."""
    persona()
    mensaje(f"<@{DUENO}> hola")
    eventos.publicar_pendientes(lector)
    assert "Say hi" not in slack[0]["json"] and "LinkedIn" not in slack[0]["json"]
    assert "|Open in Slack> to reply" in slack[0]["json"]
    assert modelo.llamadas == []


def test_an_introduction_asks_to_say_hi_with_a_greeting_ready_to_copy(
    conn, lector, slack, restore_config, modelo
):
    con_canal_de_intros(conn)
    persona()
    mensaje("Hi everyone, I run a logistics company with 40 trucks", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 1
    textos = [b["text"]["text"] for b in bloques(slack[0]) if b["type"] == "section"]
    assert "say hi" in textos[0]
    assert "Say hi" in textos[1] and "Suggested greeting" in textos[1]
    # El saludo va solo en su bloque, como código: se copia de un gesto.
    assert textos[2] == f"```{SALUDO}```"
    # Y lo que la persona dijo llegó al modelo, dentro de la valla.
    assert "40 trucks" in modelo.llamadas[0]["prompt"]
    assert (
        db.fetch_one("select saludo from eventos_canal where tipo = 'presentacion'")["saludo"]
        == SALUDO
    )


def test_the_introduction_button_opens_the_intro(conn, lector, slack, restore_config):
    con_canal_de_intros(conn)
    ts = mensaje("Hi everyone", canal=CANAL)
    eventos.publicar_pendientes(lector)
    (boton,) = bloques(slack[0])[-1]["elements"]
    assert boton["text"]["text"] == "Open intro in Slack"
    assert boton["url"] == lector.permalink(CANAL, ts)
    # Solo abre un enlace: no hay nada que mande un mensaje al club.
    assert "value" not in boton


@pytest.mark.parametrize(
    "respuesta",
    [
        RuntimeError("openai caído"),
        {"saludo": "Welcome! Verify at https://evil.co/verify"},
        {"saludo": "Welcome <!channel>"},
        "not json",
    ],
)
def test_without_a_valid_greeting_the_alert_still_goes_out(
    conn, lector, slack, restore_config, modelo, respuesta
):
    """Si el modelo falla o la validación rechaza, el aviso sale igual, sin sugerencia."""
    con_canal_de_intros(conn)
    persona()
    modelo.respuesta = respuesta
    mensaje("Hi everyone, I run a logistics company", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 1
    aviso = slack[0]["json"]
    assert "NEW INTRODUCTION" in aviso and "Say hi" in aviso
    assert "Suggested greeting" not in aviso and "evil.co" not in aviso and "```" not in aviso
    assert "Connect on LinkedIn" in aviso


def test_with_the_switch_off_there_is_no_greeting_and_no_model_call(
    conn, lector, slack, restore_config, modelo
):
    con_canal_de_intros(conn)
    db.execute("update config set value = 'false'::jsonb where key = 'saludo_sugerido_activo'")
    mensaje("Hi everyone, I run a logistics company", canal=CANAL)
    assert eventos.publicar_pendientes(lector) == 1
    assert "Suggested greeting" not in slack[0]["json"] and "Say hi" in slack[0]["json"]
    assert modelo.llamadas == []


def test_the_greeting_is_not_paid_twice_when_the_post_fails(
    conn, lector, slack, restore_config, modelo, monkeypatch
):
    con_canal_de_intros(conn)
    mensaje("Hi everyone, I run a logistics company", canal=CANAL)

    def boom(blocks, text):
        raise RuntimeError("slack caído")

    with monkeypatch.context() as m:
        m.setattr(eventos.slack_writer, "post_card", boom)
        assert eventos.publicar_pendientes(lector) == 0
    assert filas(eventos.PRESENTACION)[0]["estado"] == "pendiente"
    # Con Slack de vuelta sale, con el mismo saludo y sin otra llamada al modelo.
    assert eventos.publicar_pendientes(lector) == 1
    assert SALUDO in slack[0]["json"]
    assert len(modelo.llamadas) == 1


def test_a_backtick_in_the_greeting_cannot_close_the_code_block(
    conn, lector, slack, restore_config, modelo
):
    con_canal_de_intros(conn)
    modelo.respuesta = {"saludo": "Welcome Mike! ``` *bold* & more"}
    mensaje("Hi everyone", canal=CANAL)
    eventos.publicar_pendientes(lector)
    bloque = next(
        b["text"]["text"]
        for b in bloques(slack[0])
        if b["type"] == "section" and b["text"]["text"].startswith("```")
    )
    assert bloque == "```Welcome Mike! ''' *bold* &amp; more```"


def test_the_introduction_links_the_linkedin_profile_from_the_dossier(
    conn, lector, slack, restore_config
):
    con_canal_de_intros(conn)
    pid = persona()
    dossier(pid, {"persona": {"cargo": "CEO", "fuente": "https://www.linkedin.com/in/mike-black"}})
    mensaje("Hi everyone", canal=CANAL)
    eventos.publicar_pendientes(lector)
    assert "<https://www.linkedin.com/in/mike-black|Open their profile>" in slack[0]["json"]


def test_without_a_profile_the_introduction_links_a_people_search(
    conn, lector, slack, restore_config
):
    """Montada por código con su nombre y su empresa, y diciendo que es una búsqueda."""
    con_canal_de_intros(conn)
    pid = persona()
    dossier(pid, {"persona": {"cargo": "CEO", "fuente": "https://acme.com/team"}})
    mensaje("Hi everyone", canal=CANAL)
    eventos.publicar_pendientes(lector)
    aviso = slack[0]["json"]
    assert "linkedin.com/search/results/people/?keywords=Mike+Black+Acme|Find them" in aviso
    assert "not a confirmed profile" in aviso


def test_someone_without_a_file_is_named_from_their_slack_profile(
    conn, slack, restore_config, modelo
):
    """Quien se presenta suele ser nuevo y aún no tiene ficha: el nombre sale de su
    perfil de Slack, y con él el saludo y la búsqueda de LinkedIn."""
    con_canal_de_intros(conn)
    lector = FakeReader(owner=DUENO)
    lector.add_profile("UNUEVA", "Dana Reyes", title="COO at Northwind")
    mensaje("Hi all, Dana here", canal=CANAL, user="UNUEVA")
    assert eventos.publicar_pendientes(lector) == 1
    aviso = slack[0]["json"]
    assert "*Dana Reyes* — Northwind" in aviso and "UNUEVA" not in aviso
    assert "keywords=Dana+Reyes+Northwind" in aviso
    assert "Name: Dana Reyes" in modelo.llamadas[0]["prompt"]
    assert slack[0]["text"] == "New introduction: Dana Reyes — say hi"


def test_a_hostile_profile_name_cannot_ping_the_channel(conn, slack, restore_config):
    """El nombre sale de un perfil ajeno: se escapa en el aviso y en la notificación."""
    con_canal_de_intros(conn)
    lector = FakeReader(owner=DUENO)
    lector.add_profile("UMALO", "<!channel> Eve")
    mensaje("Hi all", canal=CANAL, user="UMALO")
    eventos.publicar_pendientes(lector)
    assert "<!channel>" not in slack[0]["json"] and "<!channel>" not in slack[0]["text"]
    assert "&lt;!channel&gt; Eve" in slack[0]["json"]
