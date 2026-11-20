"""El aviso de miembro nuevo: alguien entró al club, toca darle la bienvenida (0037).

El watcher ya detectaba a los miembros nuevos comparando padrones y encolaba su
research, pero en Slack no se decía nada: si el dossier salía con poco encaje, Anthony
nunca se enteraba de que había entrado alguien. Este aviso es otra cosa que la tarjeta
de research: no dice "aquí hay una venta", dice "ha entrado alguien, salúdale", y sale
tenga el encaje que tenga.

**Cuándo sale: al terminar el research, o tras una espera si el research no llega.**
Se apunta al detectar (`apuntar`, desde el watcher) y se publica después
(`publicar_pendientes`, desde el bucle), cuando la persona ya no tiene research abierto
o cuando pasan `miembro_nuevo_espera_min` minutos, lo que ocurra antes.

- Avisar al detectar sería inmediato, pero en ese momento solo hay un código de usuario:
  ni cargo, ni empresa, ni LinkedIn. El aviso saldría casi siempre con un enlace de
  búsqueda y sin contexto, y minutos después el research tendría justo lo que faltaba.
- Avisar solo al terminar el research ataría el aviso a algo que puede no pasar: la
  cola tiene tope por hora, el research se detiene con el kill switch o el tope mensual,
  y puede fallar. Una bienvenida no puede depender de eso.

Así que se espera lo razonable (una bienvenida no es cosa de minutos) y, si el research
no llegó, el aviso sale igual con el nombre del perfil de Slack y la búsqueda.

Lo que comparte con el resto de avisos:

- **Sin primera foto.** `apuntar` solo se llama cuando el canal ya tenía un padrón con
  el que comparar; el primero es línea base y no anuncia a nadie (ver el watcher).
- **Una vez por persona**, para siempre (`miembros_nuevos.slack_user_id` es único), y
  solo si de verdad es nueva: quien ya tiene ficha o ya estaba en otro canal vigilado
  no es un miembro nuevo del club, solo ha entrado en un canal más.
- Se reserva antes de publicar, para que un segundo worker o un reintento no dupliquen.
- Tope diario (`miembros_nuevos_por_dia`), contado en `zona_horaria`.
- Nunca a un descartado, ni a quien está en modo de pruebas, ni a un bot.
- Lo que no salió en dos días ya no sale: "nuevo" deja de ser verdad.
- Nada de esto puede propagarse: avisar es un extra y no puede tumbar el bucle.

El agente no le escribe a nadie: el aviso va al Slack de Handoff.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import canales, db, db_config, ledger
from ..config import load_settings
from ..delivery import bands, card, linkedin, prioridad, slack_writer
from ..ingest.profile_hints import company_from_title
from . import eventos

logger = logging.getLogger(__name__)

ACCION_PUBLICADO = "miembro_nuevo_publicado"
ACCION_FALLIDO = "miembro_nuevo_fallido"

CABECERA = "🆕 *NEW MEMBER · welcome them*"

POR_DIA = 20
ESPERA_MIN = 30
# Pasado esto, quien quedó sin avisar (tope diario alcanzado, bot de Handoff sin
# configurar durante días) ya no se anuncia: decir "nuevo" de alguien que lleva dos
# días dentro es peor que callar.
CADUCA_HORAS = 48


def _inicio_del_dia(ahora: datetime) -> datetime:
    zona = ZoneInfo(db_config.value("zona_horaria", "America/New_York"))
    return ahora.astimezone(zona).replace(hour=0, minute=0, second=0, microsecond=0)


def _ya_estaba(slack_user_id: str, channel_id: str) -> bool:
    """Si a esta persona ya se la conocía: tiene ficha, o figura en el último padrón de
    otro canal vigilado. Entrar en un canal más no la convierte en miembro nuevo."""
    return (
        db.fetch_one(
            """
            select 1 as hay from prospects where slack_user_id = %(uid)s
            union all
            select 1 from (
                select distinct on (channel_id) members
                  from member_snapshots
                 where channel_id <> %(canal)s
                 order by channel_id, taken_at desc
            ) ultimos
             where %(uid)s = any(ultimos.members)
            limit 1
            """,
            {"uid": slack_user_id, "canal": channel_id},
        )
        is not None
    )


def apuntar(slack_user_id: str, channel_id: str) -> bool:
    """Deja apuntado que esta persona acaba de aparecer en el padrón de `channel_id`.

    No publica nada. Devuelve True si quedó apuntada: False si ya lo estaba o si no es
    nueva de verdad. Nunca lanza: lo llama el watcher en mitad de la lectura, y un fallo
    aquí no puede costar ni el research de la persona ni la instantánea del padrón."""
    try:
        if _ya_estaba(slack_user_id, channel_id):
            return False
        return bool(
            db.execute(
                "insert into miembros_nuevos (slack_user_id, channel_id) values (%s, %s) "
                "on conflict (slack_user_id) do nothing",
                (slack_user_id, channel_id),
            )
        )
    except Exception:
        logger.exception("miembro nuevo %s: no se pudo apuntar", slack_user_id)
        return False


def _publicados_hoy(ahora: datetime) -> int:
    fila = db.fetch_one(
        "select count(*) as n from miembros_nuevos "
        "where estado = 'publicada' and publicado_at >= %s",
        (_inicio_del_dia(ahora),),
    )
    return fila["n"] if fila else 0


def _caducar(ahora: datetime) -> None:
    db.execute(
        "update miembros_nuevos set estado = 'omitida', motivo = 'caducado' "
        "where estado = 'pendiente' and created_at < %s",
        (ahora - timedelta(hours=CADUCA_HORAS),),
    )


def _listos(ahora: datetime, limite: int) -> list[dict]:
    """Los apuntados a los que ya les toca: sin research abierto, o con la espera
    cumplida. Un research que falló y espera su reintento sigue abierto, así que también
    cuenta como "todavía no" hasta que venza la espera."""
    espera = db_config.value("miembro_nuevo_espera_min", ESPERA_MIN)
    return db.fetch_all(
        """
        select m.id::text as id, m.slack_user_id, m.channel_id,
               c.nombre as canal_nombre,
               p.id::text as prospect_id, p.full_name, p.company_name,
               p.company_name_override, p.research_links, p.state, p.sin_tarjeta
          from miembros_nuevos m
          left join canales c on c.channel_id = m.channel_id
          left join prospects p on p.slack_user_id = m.slack_user_id
         where m.estado = 'pendiente'
           and (
                m.created_at <= %s
                or not exists (
                    select 1 from research_jobs j
                     where j.slack_user_id = m.slack_user_id
                       and j.status in ('pendiente', 'en_curso')
                )
           )
         order by m.created_at
         limit %s
        """,
        (ahora - timedelta(minutes=espera), limite),
    )


def _omitir(miembro_id: str, motivo: str) -> None:
    db.execute(
        "update miembros_nuevos set estado = 'omitida', motivo = %s "
        "where id = %s and estado = 'pendiente'",
        (motivo, miembro_id),
    )


def _reservar(miembro_id: str, ahora: datetime) -> bool:
    """Marca la fila como publicada antes de publicar, para no duplicar."""
    return bool(
        db.execute(
            "update miembros_nuevos set estado = 'publicada', publicado_at = %s "
            "where id = %s and estado = 'pendiente'",
            (ahora, miembro_id),
        )
    )


def _liberar(miembro_id: str, motivo: str) -> None:
    db.execute(
        "update miembros_nuevos set estado = 'pendiente', publicado_at = null, motivo = %s "
        "where id = %s",
        (motivo, miembro_id),
    )


def _identidad(fila: dict, reader) -> tuple[str | None, str | None, bool]:
    """(nombre, empresa, es_bot). Con ficha, lo de la ficha: el research ya leyó el
    perfil y ya descartó a los bots. Sin ficha se lee el perfil de Slack (`users.info`,
    una lectura): de ahí sale el nombre y, de paso, si es un bot o una cuenta borrada, a
    los que no se da la bienvenida. Si Slack no contesta, el aviso sale sin nombre."""
    nombre = (fila.get("full_name") or "").strip() or None
    empresa = (fila.get("company_name_override") or fila.get("company_name") or "").strip() or None
    if nombre:
        return nombre, empresa, False
    try:
        perfil = reader.user_profile(fila["slack_user_id"])
    except Exception:  # noqa: BLE001 - el nombre es un extra del aviso
        logger.warning("miembro nuevo %s: sin perfil de Slack", fila["slack_user_id"])
        return None, empresa, False
    if perfil.is_bot or perfil.deleted:
        return None, None, True
    nombre = (perfil.real_name or "").strip() or None
    return nombre, empresa or company_from_title(perfil.title), False


def _linea_dossier(fila: dict, dossier: dict) -> str:
    """Una línea de contexto: qué sabe ya el research de esta persona, con el enlace a su
    ficha. Sin dossier se dice, para que nadie lea el silencio como "no encaja"."""
    if not dossier:
        return "Not researched yet — a research card follows if they turn out to be a fit."
    encaje = dossier.get("encaje_handoff")
    nota = card._safe_number(
        encaje.get("puntuacion") if isinstance(encaje, dict) else None, card.NUM_CHARS
    )
    bits = [f"Dossier: fit {nota}/3" if nota else "Dossier ready"]
    panel_url = load_settings().panel_url
    if panel_url and fila.get("prospect_id"):
        enlace = f"{panel_url}/personas/{fila['prospect_id']}"
        if card._is_safe_link(enlace):
            bits.append(f"<{enlace}|Open in panel>")
    return " · ".join(bits)


def _nivel(dossier: dict) -> str | None:
    """Cuánto corre la bienvenida (0039): "today" si el research ya dice que encaja,
    "FYI" si no encaja o aún no se sabe. Lo decide `prioridad.py`."""
    banda = bands.band_for(dossier) if dossier else None
    return prioridad.visible(prioridad.nivel(prioridad.MIEMBRO_NUEVO, banda=banda))


def _aviso(
    fila: dict,
    nombre: str | None,
    empresa: str | None,
    dossier: dict,
    nivel: str | None = None,
) -> tuple[list[dict], str]:
    persona = dossier.get("persona") if isinstance(dossier.get("persona"), dict) else {}
    cargo = card._escape_and_cap(persona.get("cargo"), card.ROLE_CHARS)
    empresa_visible = card._escape_and_cap(empresa, card.COMPANY_CHARS)
    nombre_visible = eventos.visible(fila, nombre)
    detalle = ", ".join(b for b in (cargo, empresa_visible) if b)
    canal = canales.etiqueta(fila["channel_id"], fila.get("canal_nombre"))

    cabecera = [
        prioridad.cabecera(CABECERA, nivel),
        f"*{nombre_visible}*" + (f" — {detalle}" if detalle else ""),
        f"just joined {canal}",
    ]
    bloques = [
        eventos.seccion("\n".join(cabecera)),
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _linea_dossier(fila, dossier)}],
        },
        eventos.seccion(
            "👉 *Welcome them* — send a short DM or say hi in the channel while they are new."
        ),
    ]
    conectar = linkedin.linea(dossier, nombre, empresa, fila.get("research_links"))
    if conectar:
        bloques.append(eventos.seccion(conectar))
    return bloques, prioridad.resumen(f"New member: {nombre_visible} — welcome them", nivel)


def _publicar(ahora: datetime, reader) -> int:
    _caducar(ahora)
    cupo = db_config.value("miembros_nuevos_por_dia", POR_DIA) - _publicados_hoy(ahora)
    if cupo <= 0:
        return 0
    publicados = 0
    for fila in _listos(ahora, cupo):
        if fila.get("state") == "descartado":
            _omitir(fila["id"], "descartado")
            continue
        if fila.get("sin_tarjeta"):
            _omitir(fila["id"], "modo de pruebas")
            continue
        nombre, empresa, es_bot = _identidad(fila, reader)
        if es_bot:
            _omitir(fila["id"], "bot o usuario eliminado")
            continue
        if not _reservar(fila["id"], ahora):
            continue
        try:
            # El montaje también va dentro del `try`: si algo falla al construir el
            # aviso, la reserva se libera igual que si fallara Slack.
            dossier = eventos.dossier_de(fila.get("prospect_id"))
            bloques, resumen = _aviso(fila, nombre, empresa, dossier, _nivel(dossier))
            _, ts = slack_writer.post_card(bloques, resumen)
        except Exception as exc:
            logger.exception("miembro nuevo %s: no se pudo publicar", fila["slack_user_id"])
            _liberar(fila["id"], f"{type(exc).__name__}: {exc}")
            ledger.record_action(ACCION_FALLIDO, {"motivo": type(exc).__name__})
            continue
        db.execute("update miembros_nuevos set aviso_ts = %s where id = %s", (ts, fila["id"]))
        ledger.record_action(
            ACCION_PUBLICADO,
            {"canal": fila["channel_id"], "con_dossier": bool(dossier)},
            prospect_id=fila.get("prospect_id"),
        )
        publicados += 1
    return publicados


def publicar_pendientes(reader, now: datetime | None = None) -> int:
    """Publica los avisos de miembro nuevo a los que ya les toca. Devuelve cuántos
    salieron. Nunca lanza: avisar es un extra y no puede tumbar el bucle."""
    try:
        settings = load_settings()
        if not (settings.handoff_bot_token and settings.handoff_channel_id):
            return 0
        return _publicar(now or datetime.now(UTC), reader)
    except Exception:
        logger.exception("miembros nuevos: fallo; se reintenta en la vuelta siguiente")
        return 0
