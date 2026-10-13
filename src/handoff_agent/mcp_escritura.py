"""Herramientas de ESCRITURA del MCP remoto: cambiar un estado, ayudar al research,
volver a investigar, ajustar un umbral y gestionar los canales vigilados. El mismo
control que tiene el panel, y nada más.

Se registran en `mcp_remoto.py` junto a las de lectura (que siguen sin poder escribir:
corren en una transacción `READ ONLY`). Aquí cada herramienta pasa por `_escribir`, el
único camino a la base, que impone en este orden:

1. Quién llama. La identidad (correo y sufijo del token) la fija la puerta
   (`mcp_identidad`); sin ella la herramienta falla antes de tocar nada.
2. Que ese correo esté en `panel_users` (`_mcp_exigir_panel`, migración 0024). Va antes
   de buscar a la persona, para que un correo ajeno no pueda averiguar quién existe.
3. El tope diario de escrituras por persona (`mcp_escrituras_por_dia`), aparte del tope
   general de llamadas. Un candado de la base serializa a quien llama dos veces a la vez,
   así que no se cuela ninguna por encima.
4. La escritura, que la hace una función SQL `mcp_*` (migraciones 0024 y 0025) con el
   correo como primer argumento. Replican las validaciones del panel (las de research
   comparten código con `panel_ayudar_research`, y las de los canales con
   `panel_guardar_canales`). Nada de SQL de escritura vive aquí salvo la auditoría.
5. La auditoría: una fila `mcp_escritura` en `agent_actions` con quién, qué herramienta y
   el antes y el después. Va en LA MISMA transacción que el cambio: o quedan las dos cosas
   o ninguna. Nunca lleva el token (esta capa ni siquiera lo conoce).

Los canales vigilados tienen una regla más que el panel no necesitaba: el canal donde el
bot publica las tarjetas no puede entrar en la lista (también lo impide
`delivery/slack_writer.py`). Vive en el entorno del worker, así que viaja a la función SQL
como argumento, y se comprueba en los dos sitios.

Lo que NO hay y no debe haber: borrar, tocar `kill_switch` ni los presupuestos, publicar
en Slack. `tests/test_mcp_remoto.py` lo vigila (lista blanca de herramientas, de
importaciones y de SQL de este módulo).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from . import canales, db, mcp_identidad, untrusted
from .config import load_settings

logger = logging.getLogger(__name__)

ACCION = "mcp_escritura"
TOPE_POR_DEFECTO = 100
MOTIVO_MAXIMO = 500

# Lo que el panel deja poner a mano (política panel_cambia_estado, 0006). 'incompleto' lo
# decide el research, no una persona.
ESTADOS_EDITABLES = ("nuevo", "investigado", "contactado", "cliente", "descartado")

# Los umbrales numéricos que el panel ya deja tocar, menos los dos topes del propio MCP.
# La función SQL `mcp_ajustar_umbral` es la que manda (con su máximo por clave); esta lista
# da el error claro y la descripción de la herramienta, y un test comprueba que coinciden.
UMBRALES = (
    "banda_alta_min",
    "banda_media_min",
    "banda_baja_min",
    "research_por_hora",
    "sms_por_dia",
    "sms_hora_inicio",
    "sms_hora_fin",
    "radar_auto_min",
    "radar_horas_entre_escaneos",
    "unipile_busquedas_por_dia",
    "unipile_perfiles_por_dia",
    "alertas_por_dia",
    "alertas_dias_misma_persona",
    "alertas_edad_maxima_horas",
    "aviso_token_horas",
)

# Canales vigilados: la clave de config de la migración 0016. El formato y el máximo
# viven en `canales`, que también los usa la lectura.
CLAVE_CANALES = "slack_channel_ids"

RESULTADOS_REINVESTIGAR = {
    "encolado": "queued",
    "ya_en_cola": "already queued",
    "descartado": "not queued: the person is discarded",
}


@dataclass
class Resultado:
    """Lo que devuelve una operación de escritura a `_escribir`."""

    respuesta: dict  # lo que ve Claude
    cambio: dict  # el antes y el después, para la auditoría
    estado: dict = field(default_factory=dict)  # lo que quedó guardado (columna `result`)
    prospect_id: str | None = None
    clave: str | None = None


def _f(texto: object, origen: str) -> str | None:
    """Texto escrito por una persona, delimitado como en las herramientas de lectura."""
    if texto is None or str(texto).strip() == "":
        return None
    return untrusted.fence(str(texto), origen)


def _valor(cur, clave: str) -> object:
    """Un valor de config, leído con el cursor de la transacción en curso.

    No se usa `db_config.value`: pide su propia conexión al pool, y una escritura que ya
    tiene una abierta (la de la transacción) se quedaría esperando otra; con varias
    escrituras a la vez el pool se agotaría y todas esperarían a todas."""
    cur.execute("select value from config where key = %s", (clave,))
    fila = cur.fetchone()
    return fila["value"] if fila else None


def _tope(cur) -> int:
    valor = _valor(cur, "mcp_escrituras_por_dia")
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        return TOPE_POR_DEFECTO
    return max(0, int(valor))


def _exigir_tope(cur, email: str, sufijo: str) -> None:
    zona = mcp_identidad.zona_desde(_valor(cur, "zona_horaria"))
    hoy = datetime.now(zona).date().isoformat()
    cur.execute(
        """
        select count(*) as n from agent_actions
         where action = %(accion)s
           and payload ->> 'email' = %(email)s
           and (created_at at time zone %(zona)s)::date = %(hoy)s::date
        """,
        {"accion": ACCION, "email": email, "zona": zona.key, "hoy": hoy},
    )
    hechas = cur.fetchone()["n"]
    tope = _tope(cur)
    if hechas >= tope:
        logger.warning("mcp: tope de escrituras alcanzado (%s ...%s)", email, sufijo)
        raise ToolError(
            f"daily write limit reached ({tope} changes per person per day); "
            "reads still work, and writes resume tomorrow"
        )


def _error_de_base(exc: psycopg.Error, email: str, sufijo: str) -> ToolError:
    """Traduce un fallo de la base a un error que Claude pueda leer, sin filtrar nada más."""
    if isinstance(exc, psycopg.errors.RaiseException):
        mensaje = exc.diag.message_primary or "the change was rejected"
        if mensaje == "not authorized":
            logger.warning(
                "mcp: escritura rechazada, correo fuera del panel (%s ...%s)", email, sufijo
            )
            return ToolError(
                "not authorized: the owner of this token is not on the panel team, "
                "so it cannot change anything"
            )
        return ToolError(mensaje)
    logger.error("mcp: fallo de la base en una escritura: %s", type(exc).__name__)
    return ToolError("the database could not save the change; nothing was changed")


def _escribir(herramienta: str, operacion: Callable[[Any, str], Resultado]) -> dict:
    """El único camino a la base de las herramientas de escritura (ver la cabecera)."""
    email, sufijo = mcp_identidad.identidad()
    try:
        with db.transaction() as cur:
            # 1. Autorización, antes que nada: un correo ajeno no averigua nada.
            cur.execute("select public._mcp_exigir_panel(%s)", (email,))
            # 2. Un candado por persona: dos llamadas a la vez no se cuelan por encima.
            cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"mcp_escrituras:{email}",))
            _exigir_tope(cur, email, sufijo)
            # 3. La escritura y 4. su auditoría, en la misma transacción.
            resultado = operacion(cur, email)
            payload = {
                "herramienta": herramienta,
                "email": email,
                "sufijo": sufijo,
                "cambio": resultado.cambio,
            }
            if resultado.prospect_id is not None:
                payload["prospect_id"] = resultado.prospect_id
            if resultado.clave is not None:
                payload["clave"] = resultado.clave
            cur.execute(
                "insert into agent_actions (prospect_id, action, payload, result) "
                "values (%s, %s, %s::jsonb, %s::jsonb)",
                (
                    resultado.prospect_id,
                    ACCION,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    json.dumps(resultado.estado, ensure_ascii=False, default=str),
                ),
            )
    except psycopg.Error as exc:
        raise _error_de_base(exc, email, sufijo) from None
    return resultado.respuesta


def _persona(cur, referencia: str, bloquear: bool = False) -> dict:
    """La persona por id o por slack_user_id, o un error claro."""
    referencia = (referencia or "").strip()
    cur.execute(
        "select id, slack_user_id, full_name, state, company_name_override, "
        "company_domain_override, research_links, research_notes from prospects "
        "where id::text = %(r)s or slack_user_id = %(r)s limit 1"
        + (" for update" if bloquear else ""),
        {"r": referencia},
    )
    fila = cur.fetchone()
    if fila is None:
        raise ToolError("no person found with that id or Slack user id")
    return fila


def _quien(p: dict, estado: str | None = None) -> dict:
    return {
        "id": str(p["id"]),
        "slack_user_id": p["slack_user_id"],
        "nombre": _f(p["full_name"], "nombre-en-slack"),
        "estado": estado or p["state"],
    }


def persona_cambiar_estado(id_o_slack_user_id: str, estado: str, motivo: str | None = None) -> dict:
    """CAMBIA el estado de una persona (lo mismo que el selector del panel). Estados:
    nuevo, investigado, contactado, cliente, descartado. ('incompleto' lo pone el research,
    no se puede fijar a mano.) Acepta el id de `personas_buscar` o el slack_user_id.
    `motivo` es opcional y queda en el registro de auditoría ("no encaja", etc.). Úsala solo
    cuando la persona del equipo lo pida de forma explícita. Devuelve el estado anterior y
    el estado en que quedó."""
    estado = (estado or "").strip().lower()
    if estado not in ESTADOS_EDITABLES:
        extra = " ('incompleto' is set by the research itself)" if estado == "incompleto" else ""
        raise ToolError(f"invalid state{extra}; use one of: {', '.join(ESTADOS_EDITABLES)}")
    motivo = (motivo or "").strip()[:MOTIVO_MAXIMO] or None

    def operacion(cur, email: str) -> Resultado:
        p = _persona(cur, id_o_slack_user_id)
        cur.execute("select public.mcp_cambiar_estado(%s, %s, %s) as r", (email, p["id"], estado))
        r = cur.fetchone()["r"]
        cambio = {"estado": {"antes": r["estado_anterior"], "despues": r["estado"]}}
        if motivo:
            cambio["motivo"] = motivo
        return Resultado(
            respuesta={
                "ok": True,
                "persona": _quien(p, r["estado"]),
                "estado_anterior": r["estado_anterior"],
                "estado": r["estado"],
            },
            cambio=cambio,
            estado={"estado": r["estado"]},
            prospect_id=str(p["id"]),
        )

    return _escribir("persona_cambiar_estado", operacion)


def persona_ayudar_research(
    id_o_slack_user_id: str,
    empresa: str | None = None,
    web: str | None = None,
    enlaces: list[str] | None = None,
    notas: str | None = None,
) -> dict:
    """CAMBIA lo que el equipo sabe de una persona y el agente no, y vuelve a investigarla
    (lo mismo que "Help the research" del panel). Solo cambia lo que pases; lo que omitas se
    queda como está. `empresa`: nombre real de la empresa. `web`: su dominio (acme.com; vale
    una URL, se normaliza). `enlaces`: URLs que se AÑADEN a las que ya hay (máximo 10 en
    total, sin duplicados); una lista vacía [] las borra todas. `notas`: texto libre (máx.
    2000) que REEMPLAZA a las notas anteriores. Un texto vacío ("") borra empresa, web o
    notas. Si la persona no está descartada, encola un research nuevo, que gasta dinero (con
    los topes de siempre). Úsala solo cuando la persona del equipo lo pida de forma
    explícita. Devuelve lo que quedó guardado y si se encoló el research."""
    if empresa is None and web is None and enlaces is None and notas is None:
        raise ToolError("nothing to change: pass at least one of empresa, web, enlaces, notas")

    def operacion(cur, email: str) -> Resultado:
        p = _persona(cur, id_o_slack_user_id, bloquear=True)
        antes = {
            "empresa": p["company_name_override"],
            "dominio": p["company_domain_override"],
            "enlaces": list(p["research_links"] or []),
            "notas": p["research_notes"],
        }
        # Lo no indicado se conserva: la función SQL (como la del panel) guarda el estado
        # completo del formulario, y NULL o vacío significa "borra".
        enlaces_nuevos = (
            antes["enlaces"]
            if enlaces is None
            else (antes["enlaces"] + list(enlaces) if len(enlaces) else [])
        )
        cur.execute(
            "select public.mcp_ayudar_research(%s, %s, %s, %s, %s, %s) as r",
            (
                email,
                p["id"],
                antes["empresa"] if empresa is None else empresa,
                antes["dominio"] if web is None else web,
                enlaces_nuevos,
                antes["notas"] if notas is None else notas,
            ),
        )
        r = cur.fetchone()["r"]
        despues = {
            "empresa": r["empresa"],
            "dominio": r["dominio"],
            "enlaces": r["enlaces"],
            "notas": r["notas"],
        }
        cambio: dict[str, Any] = {
            campo: {"antes": antes[campo], "despues": despues[campo]}
            for campo in antes
            if antes[campo] != despues[campo]
        }
        cambio["encolado"] = r["encolado"]
        if r["encolado"]:
            research = "queued"
        elif r["estado"] == "descartado":
            research = "not queued: the person is discarded"
        else:
            research = "already queued"
        return Resultado(
            respuesta={
                "ok": True,
                "persona": _quien(p),
                "guardado": {
                    "empresa": _f(despues["empresa"], "correccion-del-equipo"),
                    "dominio": _f(despues["dominio"], "correccion-del-equipo"),
                    "enlaces": [_f(e, "enlace-del-equipo") for e in despues["enlaces"]],
                    "notas": _f(despues["notas"], "notas-del-equipo"),
                },
                "research": research,
            },
            cambio=cambio,
            estado=despues,
            prospect_id=str(p["id"]),
        )

    return _escribir("persona_ayudar_research", operacion)


def persona_reinvestigar(id_o_slack_user_id: str) -> dict:
    """ENCOLA un research nuevo para una persona (lo mismo que "Research again" del panel).
    Gasta dinero (dentro de los topes de siempre). Una persona descartada no se reencola, y
    si ya tiene una tarea pendiente o en curso no se duplica. Acepta el id de
    `personas_buscar` o el slack_user_id. Úsala solo cuando la persona del equipo lo pida de
    forma explícita. Devuelve si se encoló, ya estaba en cola o no se encoló."""

    def operacion(cur, email: str) -> Resultado:
        p = _persona(cur, id_o_slack_user_id)
        cur.execute("select public.mcp_reinvestigar(%s, %s) as r", (email, p["id"]))
        r = cur.fetchone()["r"]
        return Resultado(
            respuesta={
                "ok": True,
                "persona": _quien(p, r["estado"]),
                "resultado": r["resultado"],
                "research": RESULTADOS_REINVESTIGAR.get(r["resultado"], r["resultado"]),
            },
            cambio={"resultado": r["resultado"]},
            estado={"resultado": r["resultado"]},
            prospect_id=str(p["id"]),
        )

    return _escribir("persona_reinvestigar", operacion)


def ajustar_umbral(clave: str, valor: int) -> dict:
    """CAMBIA un umbral numérico del agente (lo mismo que Settings del panel). Claves
    permitidas: banda_alta_min, banda_media_min, banda_baja_min (0 a 4; la puntuación de
    encaje llega a 3, así que 4 silencia esa banda), research_por_hora, sms_por_dia,
    sms_hora_inicio, sms_hora_fin, radar_auto_min, radar_horas_entre_escaneos,
    unipile_busquedas_por_dia, unipile_perfiles_por_dia, alertas_por_dia,
    alertas_dias_misma_persona, alertas_edad_maxima_horas, aviso_token_horas. `valor` es un
    entero no negativo (cada clave tiene su máximo). NO se pueden tocar desde aquí el
    interruptor de emergencia ni los topes de dinero. Úsala solo cuando la persona del
    equipo lo pida de forma explícita. Devuelve el valor anterior y el nuevo."""
    clave = (clave or "").strip()
    if clave not in UMBRALES:
        raise ToolError(
            f"setting cannot be changed from here; adjustable settings: {', '.join(UMBRALES)}"
        )

    def operacion(cur, email: str) -> Resultado:
        cur.execute("select public.mcp_ajustar_umbral(%s, %s, %s) as r", (email, clave, valor))
        r = cur.fetchone()["r"]
        return Resultado(
            respuesta={"ok": True, "clave": clave, "anterior": r["anterior"], "valor": r["valor"]},
            cambio={"antes": r["anterior"], "despues": r["valor"]},
            estado={"clave": clave, "valor": r["valor"]},
            clave=clave,
        )

    return _escribir("ajustar_umbral", operacion)


def _canal_normalizado(channel_id: str) -> str:
    """El código en mayúsculas, o un error que explica dónde encontrarlo."""
    canal = (channel_id or "").strip().upper()
    if not canales.CODIGO.match(canal):
        raise ToolError(
            f"invalid channel id {(channel_id or '').strip()!r}: use the code Slack shows "
            "at the bottom of the channel details (C… or G…), not the name"
        )
    return canal


def _canales_vistos(cur, lista: list[str]) -> list[dict]:
    """Una lista de canales con el nombre de cada uno, si se conoce."""
    nombres = canales.nombres(cur, lista)
    return [
        {"canal": c, "nombre": canales.etiqueta(c, nombres[c]) if c in nombres else None}
        for c in lista
    ]


def _respuesta_canales(cur, hecho: str, canal: str, r: dict, respaldo: tuple[str, ...]) -> dict:
    respuesta = {
        "ok": True,
        hecho: canal,
        "canales": _canales_vistos(cur, r["canales"]),
        "total": len(r["canales"]),
        "maximo": canales.MAXIMO,
        "nota": "the worker picks the new list up on its next poll (a couple of minutes)",
    }
    if not r["canales"] and respaldo:
        # Una lista vacía en la base significa "sin valor del panel" y el worker vuelve a
        # SLACK_CHANNEL_IDS (ver secretos.slack_channel_ids): hay que decirlo.
        respuesta["nota"] = (
            "the watched list is now empty, so the SLACK_CHANNEL_IDS list from the "
            "worker's environment takes over again"
        )
    return respuesta


def canal_agregar(channel_id: str) -> dict:
    """AÑADE un canal de Slack a los que el agente vigila (lo mismo que Settings →
    Watched channels del panel). `channel_id` es el código que Slack muestra al final de
    los detalles del canal (empieza por C o por G; se pasa a mayúsculas), no el nombre.
    El canal donde el agente publica sus tarjetas NO se puede vigilar; un canal repetido
    se rechaza; el máximo son 20. La cuenta del token de lectura tiene que ser miembro
    del canal o el agente no podrá leerlo. Úsala solo cuando la persona del equipo lo
    pida de forma explícita. Devuelve la lista de canales resultante."""
    canal = _canal_normalizado(channel_id)
    ajustes = load_settings()
    tarjetas = (ajustes.handoff_channel_id or "").strip().upper()
    if tarjetas and canal == tarjetas:
        raise ToolError(
            f"{canal} is the channel where the agent posts its cards: it can never be "
            "watched (the agent would end up reading its own cards)"
        )

    def operacion(cur, email: str) -> Resultado:
        cur.execute(
            "select public.mcp_canal_agregar(%s, %s, %s, %s) as r",
            (email, canal, tarjetas or None, list(ajustes.slack_channel_ids)),
        )
        r = cur.fetchone()["r"]
        return Resultado(
            respuesta=_respuesta_canales(cur, "agregado", canal, r, ajustes.slack_channel_ids),
            cambio={"canales": {"antes": r["antes"], "despues": r["canales"]}},
            estado={"canales": r["canales"]},
            clave=CLAVE_CANALES,
        )

    return _escribir("canal_agregar", operacion)


def canal_quitar(channel_id: str) -> dict:
    """QUITA un canal de los que el agente vigila (lo mismo que Settings → Watched
    channels del panel). Deja de leerse en el siguiente sondeo; los mensajes y las
    señales que ese canal ya trajo se quedan donde están. Un canal que no se vigila es un
    error. Úsala solo cuando la persona del equipo lo pida de forma explícita. Devuelve la
    lista de canales resultante."""
    canal = _canal_normalizado(channel_id)
    ajustes = load_settings()

    def operacion(cur, email: str) -> Resultado:
        cur.execute(
            "select public.mcp_canal_quitar(%s, %s, %s) as r",
            (email, canal, list(ajustes.slack_channel_ids)),
        )
        r = cur.fetchone()["r"]
        return Resultado(
            respuesta=_respuesta_canales(cur, "quitado", canal, r, ajustes.slack_channel_ids),
            cambio={"canales": {"antes": r["antes"], "despues": r["canales"]}},
            estado={"canales": r["canales"]},
            clave=CLAVE_CANALES,
        )

    return _escribir("canal_quitar", operacion)
