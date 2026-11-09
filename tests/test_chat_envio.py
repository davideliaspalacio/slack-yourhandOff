"""Contestar en el Founders Club desde el panel: los cinco frenos, el permiso y el rastro.

Hasta 0034 el agente no podía escribir una palabra en ese workspace. Estos tests son el
precio de haber abierto esa puerta: cada uno fija uno de los límites que la mantienen
estrecha, y los mensajes salen con el nombre de Anthony, así que ninguno es decorativo.

Slack está siempre sustituido: ni un test de este archivo toca la red.
"""

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError

from handoff_agent import chat_envio, db, secretos
from handoff_agent.tools import respondedor
from handoff_agent.tools.respondedor import Respondedor
from handoff_agent.web import app as web_app

AUTOR = "david@yourhandoff.com"
MIRON = "otro@yourhandoff.com"
CANAL = "C0FOUNDERS"
DM = "D0LEAD"

# Lo que esta clase no puede llamar por mucho que exista el método en el SDK. Escrita a
# mano, igual que `JAMAS` en tests/test_slack_client.py: añadir uno obliga a venir aquí.
NUNCA = {
    # Escrituras que cambiarían algo de otra persona o del canal.
    "chat_update",
    "chat_delete",
    "chat_postEphemeral",
    "chat_scheduleMessage",
    "reactions_add",
    "conversations_mark",
    "conversations_setTopic",
    "conversations_join",
    "conversations_invite",
    "files_upload_v2",
    # Abrir una conversación nueva: el freno del destino existe justamente para que esto
    # no pueda pasar, y el método tampoco está disponible.
    "conversations_open",
    # Y tampoco lecturas: para leer está el lector, que es de solo lectura.
    "auth_test",
    "conversations_history",
    "conversations_replies",
    "users_info",
}


class FakeSlack:
    """Doble del WebClient. Registra cada llamada y devuelve el `ts` configurado."""

    def __init__(self, ts="1762700000.000100", error=None, respuesta=None):
        self.ts = ts
        self.error = error
        self.respuesta = respuesta
        self.calls = []

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat_postMessage", kwargs))
        if self.error:
            raise self.error
        if self.respuesta is not None:
            return self.respuesta
        return {"ok": True, "ts": self.ts, "channel": kwargs.get("channel")}


@pytest.fixture
def slack(conn, monkeypatch):
    """Sustituye el constructor del cliente, así que `_resolve_client` corre de verdad
    (incluida la regla de la cookie) pero nada sale a la red.

    `mensajes_enviados` es de 0034 y todavía no está en la lista que `conftest` vacía
    entre tests, así que se vacía aquí: el tope diario cuenta filas, y una fila heredada
    de otro test haría que estos se pisaran según el orden en que corran."""
    fake = FakeSlack()
    db.execute("truncate table mensajes_enviados")
    db.execute(
        "insert into secretos (key, value) values ('slack_user_token', 'xoxc-de-prueba'), "
        "('slack_d_cookie', 'xoxd-de-prueba') on conflict (key) do update set value = excluded.value"
    )
    monkeypatch.setattr(respondedor, "_web_client", lambda token, cookie=None: fake)
    return fake


@pytest.fixture
def listo(conn, restore_config, slack):
    """El caso bueno de partida: interruptor encendido, un autor con permiso, un canal
    vigilado y un DM que ya existe. Cada test rompe UNA de esas cosas."""
    db.execute("update config set value = 'true'::jsonb where key = 'chat_responder_activo'")
    db.execute(
        "update config set value = %s::jsonb where key = 'slack_channel_ids'", (f'["{CANAL}"]',)
    )
    db.execute("insert into panel_users (email, puede_responder) values (%s, true)", (AUTOR,))
    db.execute("insert into panel_users (email, puede_responder) values (%s, false)", (MIRON,))
    db.execute(
        "insert into dm_rastro (dm_id, team_id, slack_user_id, ultimo_ts, ultimo_de) "
        "values (%s, 'T0FOUNDERS', 'U0LEAD', '1762600000.000100', 'otro')",
        (DM,),
    )
    return slack


def filas() -> list[dict]:
    return db.fetch_all("select * from mensajes_enviados order by created_at")


# ---------------------------------------------------------------------------------
# El camino bueno, y lo que queda escrito
# ---------------------------------------------------------------------------------


def test_un_mensaje_sale_y_queda_registrado_con_su_autor_de_verdad(listo):
    """En Slack el mensaje va firmado por Anthony, así que la fila tiene que decir quién
    lo escribió de verdad: es la única forma de saberlo después."""
    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, "On it, giving you a hand now.")

    assert resultado["ok"] is True
    assert resultado["ts"] == "1762700000.000100"
    assert listo.calls == [
        ("chat_postMessage", {"channel": CANAL, "text": "On it, giving you a hand now."})
    ]
    (fila,) = filas()
    assert fila["autor_email"] == AUTOR
    assert fila["destino"] == CANAL
    assert fila["texto"] == "On it, giving you a hand now."
    assert fila["estado"] == "enviado"
    assert fila["enviado_ts"] == "1762700000.000100"
    assert fila["motivo"] is None
    assert fila["thread_ts"] is None


def test_el_autor_se_guarda_normalizado_y_nunca_el_del_formulario(listo):
    """`panel_users.email` vive en minúsculas (0006): el correo que llega del endpoint se
    compara y se guarda igual, para que la auditoría no tenga dos formas del mismo autor."""
    resultado = chat_envio.enviar_desde_panel(f"  {AUTOR.upper()}  ", CANAL, "hello")
    assert resultado["ok"] is True
    assert filas()[0]["autor_email"] == AUTOR


def test_una_respuesta_en_un_hilo_pasa_el_thread_ts(listo):
    """Contestar dentro de un hilo y no soltar un mensaje nuevo en el canal."""
    resultado = chat_envio.enviar_desde_panel(
        AUTOR, CANAL, "Same here, we did it last month.", thread_ts="1762600000.000900"
    )

    assert resultado["ok"] is True
    _, kwargs = listo.calls[0]
    assert kwargs["thread_ts"] == "1762600000.000900"
    assert filas()[0]["thread_ts"] == "1762600000.000900"


def test_sin_hilo_el_campo_no_se_manda_siquiera(listo):
    chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello", thread_ts="   ")
    _, kwargs = listo.calls[0]
    assert "thread_ts" not in kwargs
    assert filas()[0]["thread_ts"] is None


def test_un_dm_que_ya_existe_vale_como_destino(listo):
    resultado = chat_envio.enviar_desde_panel(AUTOR, DM, "Thanks for the intro!")
    assert resultado["ok"] is True
    assert listo.calls[0][1]["channel"] == DM


# ---------------------------------------------------------------------------------
# El interruptor y el kill switch
# ---------------------------------------------------------------------------------


def test_con_el_interruptor_apagado_no_se_manda_nada(listo):
    """Nace apagado en 0034: con el interruptor en false el chat es de solo lectura."""
    db.execute("update config set value = 'false'::jsonb where key = 'chat_responder_activo'")

    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")

    assert resultado["ok"] is False
    assert "turned off" in resultado["error"]
    assert listo.calls == []
    (fila,) = filas()
    assert fila["estado"] == "fallido"
    assert fila["enviado_ts"] is None
    assert "RespuestasApagadas" in fila["motivo"]


def test_sin_la_clave_en_config_tampoco_se_manda(listo):
    """Si la clave no existe (una base anterior a 0034), el valor por omisión es apagado."""
    db.execute("delete from config where key = 'chat_responder_activo'")
    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")
    assert resultado["ok"] is False
    assert listo.calls == []


def test_el_kill_switch_para_esto_como_todo_lo_demas(listo):
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")

    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")

    assert resultado["ok"] is False
    assert "kill switch" in resultado["error"]
    assert listo.calls == []
    assert filas()[0]["estado"] == "fallido"
    assert "KillSwitchActive" in filas()[0]["motivo"]


# ---------------------------------------------------------------------------------
# Quién puede: ser del panel no basta
# ---------------------------------------------------------------------------------


def test_un_usuario_del_panel_sin_permiso_no_puede_enviar(listo):
    """Ver el chat lo puede todo el equipo; escribir en él, una sola persona."""
    resultado = chat_envio.enviar_desde_panel(MIRON, CANAL, "hello")

    assert resultado["ok"] is False
    assert resultado["error"] == chat_envio.SIN_PERMISO
    assert listo.calls == []
    # Ni siquiera se anota: el permiso se comprueba antes de reservar la fila.
    assert filas() == []


def test_quien_no_esta_en_el_panel_tampoco(listo):
    resultado = chat_envio.enviar_desde_panel("nadie@example.com", CANAL, "hello")
    assert resultado["ok"] is False
    assert resultado["error"] == chat_envio.SIN_PERMISO
    assert listo.calls == []
    assert filas() == []


def test_sin_correo_no_se_manda(listo):
    for vacio in ("", "   ", None):
        resultado = chat_envio.enviar_desde_panel(vacio, CANAL, "hello")
        assert resultado["ok"] is False
    assert listo.calls == []


def test_quitar_el_permiso_surte_efecto_en_el_envio_siguiente(listo):
    assert chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")["ok"] is True
    db.execute("update panel_users set puede_responder = false where email = %s", (AUTOR,))
    assert chat_envio.enviar_desde_panel(AUTOR, CANAL, "again")["ok"] is False
    assert len(listo.calls) == 1


# ---------------------------------------------------------------------------------
# El destino: solo donde ya hay conversación
# ---------------------------------------------------------------------------------


def test_un_canal_que_no_se_vigila_se_rechaza(listo):
    """El freno que impide escribir en un canal cualquiera del club."""
    resultado = chat_envio.enviar_desde_panel(AUTOR, "C0OTRO", "hello")

    assert resultado["ok"] is False
    assert "watched channels" in resultado["error"]
    assert listo.calls == []
    (fila,) = filas()
    assert fila["estado"] == "fallido"
    assert fila["destino"] == "C0OTRO"
    assert "DestinoNoPermitido" in fila["motivo"]


def test_un_dm_que_no_esta_en_el_rastro_se_rechaza(listo):
    """No se puede abrir una conversación nueva con nadie."""
    resultado = chat_envio.enviar_desde_panel(AUTOR, "D0DESCONOCIDO", "hello")

    assert resultado["ok"] is False
    assert "already exist" in resultado["error"]
    assert listo.calls == []
    assert "DestinoNoPermitido" in filas()[0]["motivo"]


@pytest.mark.parametrize("destino", ["", "   ", "U0PERSONA", "C0!!", "founders-general", "X1"])
def test_un_destino_con_otra_forma_se_rechaza_sin_preguntar_a_la_base(listo, destino):
    """Un id que el check de la tabla (0034) no admitiría se corta antes de anotar, para
    que una errata diga lo que pasa en vez de "no se pudo anotar"."""
    resultado = chat_envio.enviar_desde_panel(AUTOR, destino, "hello")

    assert resultado["ok"] is False
    assert resultado["error"] == "That is not a channel or conversation you can reply to."
    assert listo.calls == []
    # Una fila con ese destino no puede existir: lo prohíbe el check de la tabla.
    assert filas() == []


def test_con_el_modo_automatico_vale_un_canal_descubierto_con_la_cuenta_dentro(listo):
    """Encendido `slack_canales_todos`, los canales vigilados salen de la tabla."""
    db.execute("update config set value = 'true'::jsonb where key = 'slack_canales_todos'")
    db.execute(
        "insert into canales (channel_id, nombre, es_miembro) values ('C0DESCUBIERTO', 'intros', true)"
    )

    assert chat_envio.enviar_desde_panel(AUTOR, "C0DESCUBIERTO", "hello")["ok"] is True
    assert listo.calls[0][1]["channel"] == "C0DESCUBIERTO"


def test_con_el_modo_automatico_un_canal_donde_no_esta_la_cuenta_no_vale(listo):
    """`es_miembro` falso: el canal se enseña, pero el agente no está dentro y nunca se
    une por su cuenta, así que tampoco escribe."""
    db.execute("update config set value = 'true'::jsonb where key = 'slack_canales_todos'")
    db.execute(
        "insert into canales (channel_id, nombre, es_miembro) values ('C0FUERA', 'otro', false)"
    )

    assert chat_envio.enviar_desde_panel(AUTOR, "C0FUERA", "hello")["ok"] is False
    assert listo.calls == []


def test_sin_el_modo_automatico_la_tabla_de_canales_no_abre_ningun_destino(listo):
    """Apagado el modo, manda la lista a mano y nada más."""
    db.execute(
        "insert into canales (channel_id, nombre, es_miembro) values ('C0DESCUBIERTO', 'x', true)"
    )
    assert chat_envio.enviar_desde_panel(AUTOR, "C0DESCUBIERTO", "hello")["ok"] is False
    assert listo.calls == []


def test_el_destino_se_normaliza_a_mayusculas(listo):
    assert chat_envio.enviar_desde_panel(AUTOR, f"  {CANAL.lower()}  ", "hello")["ok"] is True
    assert listo.calls[0][1]["channel"] == CANAL


# ---------------------------------------------------------------------------------
# El texto: siempre lo escribe una persona
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("texto", ["", "   ", "\n\t "])
def test_un_texto_vacio_se_rechaza(listo, texto):
    """No hay forma de mandar un mensaje que nadie haya escrito."""
    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, texto)

    assert resultado["ok"] is False
    assert "Write something" in resultado["error"]
    assert listo.calls == []
    assert "TextoInvalido" in filas()[0]["motivo"]


def test_un_texto_larguisimo_se_rechaza(listo):
    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, "x" * (respondedor.MAX_TEXTO + 1))

    assert resultado["ok"] is False
    assert str(respondedor.MAX_TEXTO) in resultado["error"]
    assert listo.calls == []


def test_el_texto_sale_tal_cual_lo_escribio_la_persona(listo):
    """Son sus palabras: no se recorta, no se reescribe y no pasa por ningún modelo."""
    texto = "Hey! We did this last month — happy to share how. <https://handoff.com|here>"
    assert chat_envio.enviar_desde_panel(AUTOR, CANAL, texto)["ok"] is True
    assert listo.calls[0][1]["text"] == texto
    assert filas()[0]["texto"] == texto


# ---------------------------------------------------------------------------------
# El tope diario
# ---------------------------------------------------------------------------------


def test_el_tope_diario_corta_tras_tantos_mensajes_como_diga_config(listo):
    """Para que un error no se vuelva una ráfaga con el nombre de Anthony encima."""
    db.execute("update config set value = '2'::jsonb where key = 'chat_mensajes_por_dia'")

    assert chat_envio.enviar_desde_panel(AUTOR, CANAL, "one")["ok"] is True
    assert chat_envio.enviar_desde_panel(AUTOR, CANAL, "two")["ok"] is True
    tercero = chat_envio.enviar_desde_panel(AUTOR, CANAL, "three")

    assert tercero["ok"] is False
    assert "Daily limit of 2" in tercero["error"]
    assert len(listo.calls) == 2
    estados = [f["estado"] for f in filas()]
    assert estados == ["enviado", "enviado", "fallido"]


def test_el_tope_cuenta_tambien_los_intentos_fallidos(listo):
    """Un bucle de fallos es exactamente la ráfaga que el tope existe para parar: contar
    solo los éxitos lo dejaría dar vueltas sin límite."""
    db.execute("update config set value = '2'::jsonb where key = 'chat_mensajes_por_dia'")

    # Dos intentos a un canal que no se vigila: no sale nada, pero quedan dos filas.
    chat_envio.enviar_desde_panel(AUTOR, "C0OTRO", "one")
    chat_envio.enviar_desde_panel(AUTOR, "C0OTRO", "two")
    tercero = chat_envio.enviar_desde_panel(AUTOR, CANAL, "three")

    assert tercero["ok"] is False
    assert "Daily limit" in tercero["error"]
    assert listo.calls == []


def test_un_tope_en_cero_apaga_el_envio(listo):
    db.execute("update config set value = '0'::jsonb where key = 'chat_mensajes_por_dia'")
    assert chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")["ok"] is False
    assert listo.calls == []


def test_lo_enviado_ayer_no_gasta_el_tope_de_hoy(listo):
    """El corte es la medianoche en `zona_horaria`, no una ventana de 24 horas."""
    db.execute("update config set value = '1'::jsonb where key = 'chat_mensajes_por_dia'")
    db.execute(
        "insert into mensajes_enviados (destino, autor_email, texto, estado, created_at) "
        "values (%s, %s, 'de ayer', 'enviado', %s)",
        (CANAL, AUTOR, datetime.now(UTC) - timedelta(days=2)),
    )

    assert chat_envio.enviar_desde_panel(AUTOR, CANAL, "hoy")["ok"] is True
    assert len(listo.calls) == 1


# ---------------------------------------------------------------------------------
# Cuando Slack falla: nunca un "enviado" que no lo esté
# ---------------------------------------------------------------------------------


def test_si_slack_falla_queda_un_fallido_y_ningun_enviado(listo):
    listo.error = SlackApiError("nope", {"ok": False, "error": "channel_not_found"})

    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")

    assert resultado["ok"] is False
    assert "channel_not_found" in resultado["error"]
    (fila,) = filas()
    assert fila["estado"] == "fallido"
    assert fila["enviado_ts"] is None
    assert "SlackUnavailable" in fila["motivo"]
    assert db.fetch_all("select 1 as x from mensajes_enviados where estado = 'enviado'") == []


def test_una_credencial_rechazada_no_cuenta_el_motivo_de_slack_a_la_pantalla(listo):
    """El mensaje del lector va en español y habla de pegar un token: aquí se traduce, y
    el token nunca aparece en ninguna parte."""
    listo.error = SlackApiError("nope", {"ok": False, "error": "invalid_auth"})

    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")

    assert resultado["ok"] is False
    assert resultado["error"].startswith("Slack rejected the stored credential")
    assert "xoxc" not in resultado["error"]
    assert "SlackAuthFailed" in filas()[0]["motivo"]


def test_un_ok_sin_ts_se_anota_como_fallido_y_no_como_enviado(listo):
    """Slack dijo ok pero no dio el `ts`. El mensaje puede haber salido: lo que no se
    puede es dejar una fila que diga "enviado" con un hueco donde va la prueba."""
    listo.respuesta = {"ok": True}

    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")

    assert resultado["ok"] is False
    assert filas()[0]["estado"] == "fallido"
    assert filas()[0]["enviado_ts"] is None


def test_la_fila_se_escribe_antes_de_llamar_a_slack(listo):
    """La regla de 0034: no puede existir un mensaje enviado del que no haya rastro. Si
    el proceso muriera dentro de la llamada, la fila `pendiente` ya estaría puesta."""
    vistas = {}

    def mirar(**kwargs):
        vistas["filas"] = filas()
        return {"ok": True, "ts": "1762700000.000100"}

    listo.chat_postMessage = mirar
    chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")

    (antes,) = vistas["filas"]
    assert antes["estado"] == "pendiente"
    assert antes["autor_email"] == AUTOR
    assert antes["texto"] == "hello"
    assert filas()[0]["estado"] == "enviado"


def test_enviar_desde_panel_nunca_lanza(listo, monkeypatch):
    """Quien llama es un endpoint contestando a una persona: un error aquí tiene que
    volver como texto, no como excepción."""

    class Explota:
        def enviar(self, *args, **kwargs):
            raise ValueError("algo inesperado")

    monkeypatch.setattr(chat_envio, "Respondedor", Explota)

    resultado = chat_envio.enviar_desde_panel(AUTOR, CANAL, "hello")

    assert resultado["ok"] is False
    assert resultado["error"] == "Something went wrong and the message was not sent."
    assert filas()[0]["estado"] == "fallido"
    assert "ValueError" in filas()[0]["motivo"]


# ---------------------------------------------------------------------------------
# La clase: un método y uno solo
# ---------------------------------------------------------------------------------


def test_la_lista_blanca_es_de_un_solo_metodo():
    assert set(respondedor.WRITE_METHODS) == {"chat_postMessage"}


@pytest.mark.parametrize("metodo", sorted(NUNCA))
def test_el_respondedor_rechaza_cualquier_metodo_que_no_sea_postmessage(metodo):
    """Ni reaccionar, ni editar, ni borrar, ni unirse, ni abrir una conversación nueva, ni
    leer. `_call` lo impone antes de tocar la red."""
    fake = FakeSlack()
    resp = Respondedor(client=fake)

    with pytest.raises(RuntimeError, match="solo manda un mensaje"):
        resp._call(metodo, channel=CANAL)
    assert fake.calls == []


def test_la_lista_blanca_y_la_negra_no_se_solapan():
    assert not (set(respondedor.WRITE_METHODS) & NUNCA)


def test_el_respondedor_no_es_el_lector_ni_hereda_de_el():
    """La pieza que escribe es aparte a propósito: el lector sigue siendo de solo lectura
    para siempre (ver tests/test_slack_client.py)."""
    from handoff_agent import slack_client as sc

    assert not issubclass(Respondedor, sc.FoundersClubReader)
    assert not issubclass(sc.FoundersClubReader, Respondedor)
    publicos = {n for n in dir(Respondedor) if not n.startswith("_")}
    assert publicos == {"enviar"}


def test_el_modulo_no_expone_ninguna_otra_forma_de_mandar():
    """Toda la escritura entra por la clase. El módulo no tiene ni una función pública:
    si alguien añade un atajo que mande algo sin pasar por `enviar` y sus cinco frenos,
    este test cae."""
    publicos = {n for n in dir(respondedor) if not n.startswith("_")}
    assert {n for n in publicos if inspect.isfunction(getattr(respondedor, n))} == set()


def test_un_token_de_sesion_manda_la_cookie_d(conn, restore_config, monkeypatch):
    """La misma regla que el lector, y en un solo sitio: un xoxc- solo vale con la
    cabecera de la cookie `d`."""
    monkeypatch.setattr(respondedor.secretos, "slack_user_token", lambda: "xoxc-1234-abcdef")
    monkeypatch.setattr(respondedor.secretos, "slack_d_cookie", lambda: "xoxd-galleta")

    cliente = Respondedor()._resolve_client()

    assert cliente.token == "xoxc-1234-abcdef"
    assert cliente.headers["Cookie"] == "d=xoxd-galleta"


def test_un_token_de_sesion_sin_cookie_es_un_fallo_de_credencial(conn, restore_config, monkeypatch):
    monkeypatch.setattr(respondedor.secretos, "slack_user_token", lambda: "xoxc-1234-abcdef")
    monkeypatch.setattr(respondedor.secretos, "slack_d_cookie", lambda: None)

    with pytest.raises(respondedor.SlackAuthFailed, match="cookie d"):
        Respondedor()._resolve_client()


def test_sin_token_no_hay_envio(conn, restore_config, monkeypatch):
    monkeypatch.setattr(respondedor.secretos, "slack_user_token", lambda: None)
    with pytest.raises(respondedor.SlackAuthFailed, match="SLACK_USER_TOKEN"):
        Respondedor()._resolve_client()


def test_el_respondedor_usa_la_misma_credencial_guardada_que_el_lector(conn, restore_config):
    """No hay una segunda credencial para escribir: es la sesión de Anthony, la que el
    panel guarda en Settings, y por eso los mensajes salen con su nombre."""
    db.execute(
        "insert into secretos (key, value) values ('slack_user_token', 'xoxp-guardado') "
        "on conflict (key) do update set value = excluded.value"
    )
    assert secretos.slack_user_token() == "xoxp-guardado"
    assert Respondedor()._resolve_client().token == "xoxp-guardado"


# ---------------------------------------------------------------------------------
# La puerta del panel: POST /panel/chat/enviar
#
# El panel no tiene otro camino hasta el worker (todo lo suyo pasa por PostgREST), y la
# credencial de Anthony no sale del worker. La llave es el access token de la sesión del
# panel, que se cambia por el correo de su dueño contra Supabase Auth.
# ---------------------------------------------------------------------------------

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkYXZpZCJ9.firma-que-no-se-valida-aqui"


@pytest.fixture
def cliente_web(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proyecto.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_de_prueba")
    return TestClient(web_app.app)


@pytest.fixture
def supabase(monkeypatch):
    """Sustituye la consulta a Supabase Auth. `correo` es lo que confirma para el token."""
    estado = SimpleNamespace(correo=AUTOR, llamadas=[])

    def falso_get(url, headers=None, timeout=None):
        estado.llamadas.append({"url": url, "headers": headers or {}})
        if estado.correo is None:
            return httpx.Response(401, json={"msg": "invalid token"})
        return httpx.Response(200, json={"id": "uuid", "email": estado.correo})

    monkeypatch.setattr(web_app.httpx, "get", falso_get)
    return estado


def _enviar(cliente, cuerpo, token=JWT):
    cabeceras = {"Authorization": f"Bearer {token}"} if token else {}
    return cliente.post("/panel/chat/enviar", json=cuerpo, headers=cabeceras)


def test_el_endpoint_manda_el_mensaje_de_quien_trae_la_sesion(listo, cliente_web, supabase):
    respuesta = _enviar(cliente_web, {"destino": CANAL, "texto": "hello from the panel"})

    assert respuesta.status_code == 200
    assert respuesta.json()["ok"] is True
    assert listo.calls[0][1]["text"] == "hello from the panel"
    assert filas()[0]["autor_email"] == AUTOR
    # El token se le pregunta a Supabase, no se cree a ciegas.
    assert supabase.llamadas[0]["url"].endswith("/auth/v1/user")
    assert supabase.llamadas[0]["headers"]["Authorization"] == f"Bearer {JWT}"


def test_el_autor_sale_del_token_y_nunca_del_cuerpo(listo, cliente_web, supabase):
    """Lo que de verdad protege la auditoría: el mensaje sale firmado por Anthony, así
    que quién lo escribió no puede ser un campo que el llamante rellene."""
    respuesta = _enviar(
        cliente_web,
        {"destino": CANAL, "texto": "hello", "autor_email": MIRON, "correo": MIRON},
    )

    assert respuesta.json()["ok"] is True
    assert filas()[0]["autor_email"] == AUTOR


def test_sin_cabecera_authorization_es_un_401(listo, cliente_web, supabase):
    respuesta = _enviar(cliente_web, {"destino": CANAL, "texto": "hello"}, token=None)
    assert respuesta.status_code == 401
    assert listo.calls == []
    assert filas() == []


@pytest.mark.parametrize("token", ["", "corto", "con espacios dentro del token", "a" * 5000])
def test_un_token_con_otra_forma_no_llega_ni_a_supabase(listo, cliente_web, supabase, token):
    respuesta = _enviar(cliente_web, {"destino": CANAL, "texto": "hello"}, token=token)
    assert respuesta.status_code == 401
    assert supabase.llamadas == []
    assert listo.calls == []


def test_un_token_que_supabase_no_reconoce_es_un_401(listo, cliente_web, supabase):
    """Una sesión caducada o cerrada: Supabase contesta 401 y aquí no entra nadie."""
    supabase.correo = None

    respuesta = _enviar(cliente_web, {"destino": CANAL, "texto": "hello"})

    assert respuesta.status_code == 401
    assert listo.calls == []
    assert filas() == []


def test_un_correo_del_panel_sin_permiso_recibe_un_200_con_ok_false(listo, cliente_web, supabase):
    """Un rechazo no es un error de la petición: el panel tiene que poder enseñar el
    motivo sin confundirlo con "se cayó algo"."""
    supabase.correo = MIRON

    respuesta = _enviar(cliente_web, {"destino": CANAL, "texto": "hello"})

    assert respuesta.status_code == 200
    assert respuesta.json() == {"ok": False, "error": chat_envio.SIN_PERMISO}
    assert listo.calls == []


def test_el_interruptor_apagado_tambien_vuelve_como_200_con_ok_false(listo, cliente_web, supabase):
    db.execute("update config set value = 'false'::jsonb where key = 'chat_responder_activo'")

    respuesta = _enviar(cliente_web, {"destino": CANAL, "texto": "hello"})

    assert respuesta.status_code == 200
    assert respuesta.json()["ok"] is False
    assert "turned off" in respuesta.json()["error"]
    assert listo.calls == []


def test_el_endpoint_pasa_el_thread_ts(listo, cliente_web, supabase):
    respuesta = _enviar(
        cliente_web,
        {"destino": CANAL, "texto": "hello", "thread_ts": "1762600000.000900"},
    )
    assert respuesta.json()["ok"] is True
    assert listo.calls[0][1]["thread_ts"] == "1762600000.000900"


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"texto": "hello"},
        {"destino": CANAL},
        {"destino": 1234, "texto": "hello"},
        {"destino": CANAL, "texto": None},
        [],
    ],
)
def test_un_cuerpo_que_no_es_el_esperado_es_un_400_sin_tocar_nada(
    listo, cliente_web, supabase, cuerpo
):
    respuesta = _enviar(cliente_web, cuerpo)
    assert respuesta.status_code == 400
    assert listo.calls == []
    assert filas() == []


def test_un_cuerpo_que_no_es_json_es_un_400(listo, cliente_web, supabase):
    respuesta = cliente_web.post(
        "/panel/chat/enviar",
        content=b"no soy json",
        headers={"Authorization": f"Bearer {JWT}", "content-type": "application/json"},
    )
    assert respuesta.status_code == 400
    assert listo.calls == []


def test_sin_supabase_configurado_no_entra_nadie(listo, cliente_web, supabase, monkeypatch):
    """Sin SUPABASE_URL no hay forma de saber quién llama, así que no se manda nada:
    nunca se cae al correo del cuerpo."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)

    respuesta = _enviar(cliente_web, {"destino": CANAL, "texto": "hello"})

    assert respuesta.status_code == 401
    assert supabase.llamadas == []
    assert listo.calls == []
