"""La misma persona, una sola tarjeta (el fallo del 2026-11-20).

En producción una persona recibió 12 tarjetas en 3 días: 12 tareas `miembro_nuevo`, 12
versiones de un dossier degradado y una tarjeta (y un SMS, si la banda era alta) por
cada versión. Cada test de aquí reproduce un eslabón de esa cadena, y cada eslabón se
arregla por separado: cualquiera solo deja el agujero abierto.
"""

from datetime import UTC, datetime, timedelta

import pytest

from handoff_agent import db
from handoff_agent.delivery import deliver, sms
from handoff_agent.ingest import resolver, watcher
from handoff_agent.research import reintentos
from handoff_agent.research import worker as w
from handoff_agent.research.gather import Evidence, Gathered
from handoff_agent.tools import prospects
from tests.slack_fakes import FakeReader
from tests.test_card import DOSSIER
from tests.test_dossier import make_dossier
from tests.test_worker import synth_returning

NOW = 1_726_000_000.0


def tick(reader, canal):
    return watcher.watch_tick(reader, [canal], lookback_hours=1, now=lambda: NOW)


def terminar_tareas():
    """Lo que hace el worker entre dos lecturas: la tarea abierta termina."""
    db.execute("update research_jobs set status = 'hecho', finished_at = now()")


def tareas(uid="U2"):
    return db.fetch_one("select count(*) as n from research_jobs where slack_user_id = %s", (uid,))[
        "n"
    ]


# ---------------------------------------------------------------------------------
# Capa 1: una persona se encola como miembro nuevo una vez
# ---------------------------------------------------------------------------------


def test_un_miembro_nuevo_en_varios_canales_es_una_sola_tarea(conn):
    """El modo automático revisa el padrón de cada canal a su ritmo. Quien entra al club
    aparece como "nuevo" en cada canal donde está, y para cuando se mira el segundo la
    tarea del primero ya terminó: el índice de una-tarea-abierta ya no la frena."""
    reader = FakeReader()
    for canal in ("C1", "C2", "C3"):
        reader.set_members(canal, "U1")
        tick(reader, canal)  # línea base
    for canal in ("C1", "C2", "C3"):
        reader.set_members(canal, "U1", "U2")

    assert tick(reader, "C1").members_new == 1
    terminar_tareas()
    assert tick(reader, "C2").members_new == 0
    terminar_tareas()
    assert tick(reader, "C3").members_new == 0
    assert tareas() == 1


def test_un_padron_que_parpadea_no_vuelve_a_encolar(conn):
    """Si Slack devuelve una lista de miembros incompleta, en la lectura siguiente la
    persona "vuelve a entrar". No es nueva: ya se la encoló una vez."""
    reader = FakeReader()
    reader.set_members("C1", "U1")
    tick(reader, "C1")
    reader.set_members("C1", "U1", "U2")
    tick(reader, "C1")
    terminar_tareas()
    reader.set_members("C1", "U1")
    tick(reader, "C1")
    reader.set_members("C1", "U1", "U2")
    assert tick(reader, "C1").members_new == 0
    assert tareas() == 1


def test_quien_ya_estaba_en_otro_canal_no_es_un_miembro_nuevo(conn):
    """Alguien que lleva meses en el club entra en un canal más. No se le investiga como
    recién llegado (ni se le manda la tarjeta de "new member"): si escribe, ya se le
    investigará por lo que diga."""
    reader = FakeReader()
    reader.set_members("C1", "U1", "UVETERANA")
    reader.set_members("C2", "U1")
    tick(reader, "C1")
    tick(reader, "C2")
    reader.set_members("C2", "U1", "UVETERANA")
    assert tick(reader, "C2").members_new == 0
    assert tareas("UVETERANA") == 0


def test_dos_personas_nuevas_a_la_vez_son_dos_tareas(conn):
    """El candado es por persona: no frena a nadie más."""
    reader = FakeReader()
    reader.set_members("C1", "U1")
    tick(reader, "C1")
    reader.set_members("C1", "U1", "U2", "U3")
    assert tick(reader, "C1").members_new == 2
    assert (tareas("U2"), tareas("U3")) == (1, 1)


def test_el_aviso_de_bienvenida_sigue_apuntandose_una_vez(conn):
    """El research ya no se repite, y el aviso de miembro nuevo (0037) tampoco cambia:
    se apunta al ver a la persona por primera vez y no en los canales siguientes."""
    reader = FakeReader()
    for canal in ("C1", "C2"):
        reader.set_members(canal, "U1")
        tick(reader, canal)
        reader.set_members(canal, "U1", "U2")
    tick(reader, "C1")
    tick(reader, "C2")
    filas = db.fetch_all("select slack_user_id, channel_id from miembros_nuevos")
    assert filas == [{"slack_user_id": "U2", "channel_id": "C1"}]


def test_quien_ya_tiene_ficha_no_se_encola_como_miembro_nuevo(conn):
    """Quien ya se investigó porque escribió no es un miembro nuevo por entrar en otro
    canal: ya tiene ficha."""
    prospects.upsert_prospect("U2", full_name="Ada Ruiz")
    reader = FakeReader()
    reader.set_members("C1", "U1")
    tick(reader, "C1")
    reader.set_members("C1", "U1", "U2")
    assert tick(reader, "C1").members_new == 0
    assert tareas() == 0


# ---------------------------------------------------------------------------------
# Capa 2: un dossier degradado o incompleto no se reintenta enseguida ni sin fin
# ---------------------------------------------------------------------------------


def _gather_degradado(
    pid, full_name, company, domain=None, extra_links=(), notes=None, mensajes=()
):
    return Gathered(
        "acme.com",
        [Evidence("home", "https://acme.com/", "Acme", "t")],
        [],
        ["prensa: vetado"],
        searches_attempted=2,
        searches_answered=0,
    )


@pytest.fixture
def degradado(monkeypatch):
    monkeypatch.setattr(w, "gather", _gather_degradado)
    monkeypatch.setattr(w, "run_followup", lambda pid, gathered, queries: gathered)
    synth = synth_returning(*[make_dossier()] * 10)
    monkeypatch.setattr(w, "synthesize", synth)
    return synth


def envejecer(pid, horas):
    db.execute(
        "update dossiers set created_at = created_at - make_interval(hours => %s) "
        "where prospect_id = %s",
        (horas, pid),
    )
    db.execute(
        "update agent_actions set created_at = created_at - make_interval(hours => %s) "
        "where prospect_id = %s",
        (horas, pid),
    )


def test_un_dossier_degradado_no_se_rehace_enseguida(conn, degradado):
    primero = w.research_person("Ada Ruiz", "Acme", slack_user_id="U2")
    assert primero.status == "incompleto"
    otra_vez = w.research_person("Ada Ruiz", "Acme", slack_user_id="U2")
    assert otra_vez.status == "omitido"
    assert otra_vez.version is None
    assert "reintento" in otra_vez.reason
    assert len(degradado.calls) == 1  # no se pagó otra síntesis


def test_pasado_el_plazo_se_reintenta_y_hay_un_maximo(conn, degradado):
    """24 horas entre intentos y dos reintentos automáticos: tres investigaciones como
    mucho, y después solo a mano."""
    pid = w.research_person("Ada Ruiz", "Acme", slack_user_id="U2").prospect_id
    for esperado in (2, 3):
        envejecer(pid, 25)
        assert w.research_person("Ada Ruiz", "Acme", slack_user_id="U2").version == esperado
    envejecer(pid, 25 * 30)
    agotado = w.research_person("Ada Ruiz", "Acme", slack_user_id="U2")
    assert agotado.status == "omitido"
    assert "agotados" in agotado.reason
    assert len(degradado.calls) == 3


def test_el_research_manual_fuerza_aunque_no_toque(conn, degradado):
    w.research_person("Ada Ruiz", "Acme", slack_user_id="U2")
    forzado = w.research_person("Ada Ruiz", "Acme", slack_user_id="U2", force=True)
    assert forzado.version == 2


def test_un_dossier_sano_pone_la_cuenta_a_cero(conn, degradado, monkeypatch):
    pid = w.research_person("Ada Ruiz", "Acme", slack_user_id="U2").prospect_id
    assert reintentos.estado(pid).intentos == 1
    prospects.save_dossier(pid, make_dossier(), [])
    assert reintentos.estado(pid).intentos == 0


def test_un_incompleto_en_espera_no_se_reencola_con_cada_mensaje(conn, degradado):
    """El resolver reencolaba a los `incompleto` con cada mensaje nuevo: una tarea más,
    un cupo de la hora gastado, y antes de este arreglo otro dossier y otra tarjeta."""
    pid = w.research_person("Ada Ruiz", "Acme", slack_user_id="U2").prospect_id
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) values ('C1', '1.0', 'U2', 'x')"
    )
    resultado = resolver.resolve_pending()
    assert (resultado.enqueued, resultado.to_scoring) == (0, 1)
    assert tareas() == 0

    envejecer(pid, 25)
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text) values ('C1', '2.0', 'U2', 'x')"
    )
    assert resolver.resolve_pending().enqueued == 1


# ---------------------------------------------------------------------------------
# Capa 3: una tarjeta por persona y plazo, salvo motivo real
# ---------------------------------------------------------------------------------


def persona_con_dossier(score=3):
    person = prospects.upsert_prospect("U2", full_name="Ada Ruiz")
    db.execute("update prospects set state = 'investigado' where id = %s", (person["id"],))
    otra_version(person["id"], score)
    return person


def otra_version(pid, score=3):
    dossier = {**DOSSIER, "encaje_handoff": {"puntuacion": score, "razon": "r"}}
    return prospects.save_dossier(pid, dossier, [])


@pytest.fixture
def publicadas(monkeypatch):
    salieron = []

    def post_card(blocks, text):
        salieron.append(text)
        return "CHANDOFF", f"{len(salieron)}.0"

    monkeypatch.setattr(deliver.slack_writer, "post_card", post_card)
    return salieron


def test_un_dossier_rehecho_no_es_otra_tarjeta(conn, publicadas):
    """El fallo tal cual: la misma persona, la misma banda, una versión nueva del
    dossier. Antes cada versión era una tarjeta."""
    person = persona_con_dossier()
    assert deliver.deliver_for(person["id"], FakeReader(), reason="miembro_nuevo") == "alta"
    for _ in range(3):
        otra_version(person["id"])
        # None y no "alta": el runner solo manda el SMS si aquí sale "alta".
        assert deliver.deliver_for(person["id"], FakeReader(), reason="miembro_nuevo") is None
    assert len(publicadas) == 1
    accion = db.fetch_one(
        "select count(*) as n from agent_actions where action = 'tarjeta_repetida_omitida'"
    )
    assert accion["n"] == 3


def test_la_version_nueva_queda_cubierta_por_la_tarjeta_que_ya_salio(conn, publicadas):
    """El resumen del día y Today miran la tarjeta del último dossier. La versión nueva
    no publica nada, pero queda apuntando a la tarjeta que ya salió: la persona no
    desaparece de lo pendiente por haberse reinvestigado."""
    person = persona_con_dossier()
    deliver.deliver_for(person["id"], FakeReader())
    otra_version(person["id"])
    deliver.deliver_for(person["id"], FakeReader())
    filas = db.fetch_all(
        "select dossier_version, band, channel_id, message_ts, detail from deliveries "
        "where kind = 'slack' order by dossier_version"
    )
    assert [f["dossier_version"] for f in filas] == [1, 2]
    assert (filas[1]["channel_id"], filas[1]["message_ts"]) == ("CHANDOFF", "1.0")
    assert filas[1]["detail"]["repite_de"]
    assert "repite_de" not in filas[0]["detail"]


def test_si_la_banda_sube_si_sale_tarjeta(conn, publicadas):
    person = persona_con_dossier(score=2)
    assert deliver.deliver_for(person["id"], FakeReader()) == "media"
    otra_version(person["id"], score=3)
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    assert len(publicadas) == 2


def test_si_la_banda_baja_no_sale_tarjeta(conn, publicadas):
    person = persona_con_dossier(score=3)
    deliver.deliver_for(person["id"], FakeReader())
    otra_version(person["id"], score=2)
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    assert len(publicadas) == 1


def test_el_research_pedido_a_mano_si_publica(conn, publicadas):
    person = persona_con_dossier()
    deliver.deliver_for(person["id"], FakeReader())
    otra_version(person["id"])
    assert deliver.deliver_for(person["id"], FakeReader(), reason="manual") == "alta"
    assert len(publicadas) == 2


def test_pasado_el_plazo_vuelve_a_salir(conn, publicadas, restore_config):
    person = persona_con_dossier()
    deliver.deliver_for(person["id"], FakeReader())
    db.execute("update deliveries set created_at = now() - interval '8 days'")
    otra_version(person["id"])
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    assert len(publicadas) == 2


def test_el_plazo_sale_de_config_y_cero_lo_apaga(conn, publicadas, restore_config):
    db.execute("update config set value = '0'::jsonb where key = 'tarjeta_dias_misma_persona'")
    person = persona_con_dossier()
    deliver.deliver_for(person["id"], FakeReader())
    otra_version(person["id"])
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"


def test_una_tarjeta_que_no_llego_a_salir_no_cuenta(conn, publicadas, monkeypatch):
    """Si Slack falló, la reserva se libera: la versión siguiente sí puede publicar."""
    person = persona_con_dossier()

    def falla(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(deliver.slack_writer, "post_card", falla)
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda b, t: ("CHANDOFF", "9.0"))
    otra_version(person["id"])
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"


# --- el SMS ---


@pytest.fixture
def twilio(monkeypatch):
    for nombre in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_TO"):
        monkeypatch.setenv(nombre, "x")
    enviados = []
    monkeypatch.setattr(sms, "_send", lambda to, body: enviados.append(body) or "SM1")
    monkeypatch.setattr(sms, "_card_link", lambda prospect_id: None)
    return enviados


MEDIODIA = datetime(2026, 11, 18, 17, 0, tzinfo=UTC)  # las 12 en Nueva York


def test_un_sms_por_persona_y_plazo(conn, twilio):
    person = persona_con_dossier()
    assert sms.maybe_send(person["id"], "alta", now=MEDIODIA) is True
    assert sms.maybe_send(person["id"], "alta", now=MEDIODIA + timedelta(hours=2)) is False
    assert len(twilio) == 1


def test_el_sms_de_un_research_manual_si_sale(conn, twilio):
    person = persona_con_dossier()
    sms.maybe_send(person["id"], "alta", now=MEDIODIA)
    assert sms.maybe_send(person["id"], "alta", now=MEDIODIA, forzar=True) is True


def test_pasado_el_plazo_el_sms_vuelve_a_salir(conn, twilio):
    person = persona_con_dossier()
    sms.maybe_send(person["id"], "alta", now=MEDIODIA)
    db.execute("update deliveries set created_at = %s", (MEDIODIA - timedelta(days=8),))
    assert sms.maybe_send(person["id"], "alta", now=MEDIODIA) is True
