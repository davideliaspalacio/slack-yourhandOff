"""El reporte semanal de empresas contratando (migración 0040).

AG lo pidió con estas palabras: cuando la tarjeta solo dice que la empresa tiene
vacantes abiertas, "I can't do anything", "I don't even need the alert every day"; lo
que quiere es "some weekly report that just tells me: these are the 15 companies hiring
right now", para dedicarle una hora a la semana y hacer nurture: ir a su LinkedIn,
comentar un post.

Así que las fichas cuya única evidencia son vacantes publicadas ya no salen como
tarjeta (`bands.va_al_reporte`, que es donde vive la regla) y salen aquí: un mensaje a
la semana en el canal de tarjetas de Handoff, con el bot de Handoff (nunca en el
Founders Club), el día `reporte_contratando_dia` a partir de `reporte_contratando_hora`
en `zona_horaria`.

Qué lista
---------

Las personas cuyo ÚLTIMO dossier se guardó en los últimos `reporte_contratando_dias`
días y va al reporte según `bands.va_al_reporte`, con una nota que habría dado para
tarjeta (`bands.banda_por_nota`). Fuera quedan quien ya está `contactado`, `cliente` o
`descartado`, quien está en modo de pruebas y las personas del radar de cuentas (no son
miembros del club y tienen su propio camino).

- **Una línea por empresa.** Dos miembros de la misma empresa son una línea, la de quien
  queda más arriba en el orden; la empresa se reconoce por su dominio y, si no lo hay,
  por su nombre.
- **Sin repetir de una semana a otra.** Quien salió en un reporte no vuelve a salir en
  `NO_REPETIR_DIAS`, aunque la ventana sea más larga que una semana o se le rehaga el
  dossier.
- **Lo más accionable arriba**: primero las empresas con vacantes que Handoff cubre,
  luego la nota de encaje, luego cuántas de esas vacantes hay, luego quien tiene perfil
  de LinkedIn conocido (un clic para empezar), y a igualdad lo más reciente.
- **Con tope** (`reporte_contratando_max`): las que no caben se cuentan, no se pintan.

Cada línea: empresa, el miembro del club y su cargo, cuántas vacantes y de qué (primero
los roles que Handoff cubre), su LinkedIn (`linkedin.py`: el perfil si se conoce, una
búsqueda si no, y el texto del enlace dice cuál de las dos) y su ficha en el panel.

Sin duplicar y sin soltar un atraso
-----------------------------------

- **Se reserva antes de publicar** (`reportes_contratando`, una fila por semana): dos
  workers, o un reintento, chocan con la clave primaria y solo uno publica.
- **Solo el día que toca.** Si ese día el worker estuvo parado, esa semana no hay
  reporte: no sale con retraso otro día. Desplegar esto o encender el interruptor un
  miércoles no publica nada hasta el día configurado.
- **El primer reporte mira su ventana y nada más**: no hay ninguna cola de "empresas
  pendientes de anunciar" que vaciar.
- **Sin nada que listar no se publica**, y la semana queda cerrada (`vacio`).
- **Si Slack falla** se reintenta en la pasada siguiente, hasta `MAX_INTENTOS`.
- Respeta el kill switch y el tope mensual, como el resto de la entrega.

Todo es por reglas, sin modelo: lo que se lista sale de campos que el dossier ya traía.
Nada de esto lanza: el bucle no puede caerse por un reporte.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db, db_config, guards, ledger
from ..config import load_settings
from . import bands, linkedin, prioridad, slack_writer
from .card import (
    COMPANY_CHARS,
    NAME_CHARS,
    NUM_CHARS,
    ROLE_CHARS,
    ROLES_CHARS,
    _escape_and_cap,
    _safe_number,
)
from .resumen_prioridad import ESTADOS_ATENDIDOS, _enlace, _enlace_panel, _secciones

logger = logging.getLogger(__name__)

ACCION_PUBLICADO = "reporte_contratando_publicado"
ACCION_FALLIDO = "reporte_contratando_fallido"
ACCION_PARADA = "reporte_contratando_parada"

CABECERA = "🏢 *WEEKLY HIRING REPORT · companies hiring right now*"
PIE = (
    "Companies that are only hiring no longer come as HIGH SIGNAL cards: they come here, "
    "once a week, and each company is listed once."
)

# Valores por omisión, los mismos que la migración 0040 deja en config.
DIA = 1  # lunes (ISO: 1 lunes … 7 domingo)
HORA = 9
DIAS = 7
MAXIMO = 15
# Cuántas veces se intenta publicar el reporte de una semana si Slack falla.
MAX_INTENTOS = 3
# Quien salió en un reporte no vuelve a salir hasta pasado esto.
NO_REPETIR_DIAS = 30
# Cuántos roles se nombran en una línea; el resto se cuenta ("+2 more").
MAX_ROLES = 3

_SQL_CANDIDATOS = """
select p.id::text as prospect_id, p.slack_user_id, p.full_name,
       p.company_name, p.company_name_override, p.company_domain,
       p.company_domain_override, p.research_links,
       d.version, d.content, d.created_at
  from prospects p
  join lateral (
        select x.version, x.content, x.created_at
          from dossiers x
         where x.prospect_id = p.id
         order by x.version desc
         limit 1
       ) d on true
 where d.created_at >= %(desde)s
   -- Solo para no traer todos los dossiers de la semana: quién va al reporte lo
   -- decide `bands.va_al_reporte`, más abajo.
   and d.content -> 'encaje_handoff' ->> 'evidencia' = %(evidencia)s
   and p.state::text <> all(%(atendidos)s::text[])
   and not coalesce(p.sin_tarjeta, false)
   and not exists (
        select 1 from reportes_contratando_lineas l
         where l.prospect_id = p.id and l.created_at >= %(no_repetir)s
       )
"""


@dataclass(frozen=True)
class Empresa:
    """Una línea candidata del reporte: una empresa y el miembro del club que la trae."""

    clave: str
    prospect_id: str
    version: int
    empresa: str
    nombre: str | None
    cargo: str | None
    vacantes: int
    # Los roles que Handoff cubre, y después los demás.
    cubiertos: tuple[str, ...]
    otros: tuple[str, ...]
    nota: int
    perfil: str | None
    busqueda: str | None
    cuando: datetime

    @property
    def orden(self) -> tuple:
        """Lo más accionable primero (el menor va antes)."""
        return (
            0 if self.cubiertos else 1,
            -self.nota,
            -len(self.cubiertos),
            0 if self.perfil else 1,
            -self.vacantes,
            -self.cuando.timestamp(),
        )


def _zona() -> ZoneInfo:
    return ZoneInfo(db_config.value("zona_horaria", "America/New_York"))


def activo() -> bool:
    return db_config.value("reporte_contratando_activo", True) is True


def _textos(valor: object) -> tuple[str, ...]:
    """Una lista de roles tal como la escribió el modelo: solo cadenas con algo dentro,
    sin repetir (sin distinguir mayúsculas) y en su orden."""
    vistos: dict[str, str] = {}
    for item in valor if isinstance(valor, list) else []:
        if isinstance(item, str) and item.strip():
            limpio = " ".join(item.split())
            vistos.setdefault(limpio.lower(), limpio)
    return tuple(vistos.values())


def _clave_empresa(dominio: object, nombre: str) -> str:
    """Por qué se reconoce que dos fichas son de la misma empresa: el dominio si lo hay
    (sin `www.`), y si no el nombre sin signos ni mayúsculas."""
    if isinstance(dominio, str) and dominio.strip():
        return "d:" + dominio.strip().lower().removeprefix("www.")
    return "n:" + re.sub(r"[^a-z0-9]+", "", nombre.lower())


def _empresa(fila: dict) -> Empresa | None:
    contenido = fila["content"]
    if not isinstance(contenido, dict):
        return None
    # La regla, en su sitio: quién va al reporte y con qué nota mínima.
    if not bands.va_al_reporte(contenido, fila["slack_user_id"]):
        return None
    if bands.banda_por_nota(contenido) not in prioridad.BANDAS_CON_TARJETA:
        return None

    def seccion(clave: str) -> dict:
        valor = contenido.get(clave)
        return valor if isinstance(valor, dict) else {}

    empresa, persona, hiring = seccion("empresa"), seccion("persona"), seccion("contratacion")
    nombre_empresa = next(
        (
            v.strip()
            for v in (
                fila.get("company_name_override"),
                empresa.get("nombre"),
                fila.get("company_name"),
            )
            if isinstance(v, str) and v.strip()
        ),
        None,
    )
    if not nombre_empresa:
        # Una línea sin empresa no dice a quién hacer seguimiento.
        return None
    vacantes = hiring.get("vacantes_abiertas")
    if type(vacantes) is not int or vacantes <= 0:
        return None
    cubiertos = _textos(hiring.get("roles_deslocalizables"))
    ya = {r.lower() for r in cubiertos}
    otros = tuple(r for r in _textos(hiring.get("roles")) if r.lower() not in ya)
    nombre = next(
        (
            v.strip()
            for v in (fila.get("full_name"), persona.get("nombre"))
            if isinstance(v, str) and v.strip()
        ),
        None,
    )
    cargo = persona.get("cargo")
    dominio = (
        fila.get("company_domain_override") or fila.get("company_domain") or empresa.get("dominio")
    )
    return Empresa(
        clave=_clave_empresa(dominio, nombre_empresa),
        prospect_id=fila["prospect_id"],
        version=fila["version"],
        empresa=nombre_empresa,
        nombre=nombre,
        cargo=cargo.strip() if isinstance(cargo, str) and cargo.strip() else None,
        vacantes=vacantes,
        cubiertos=cubiertos,
        otros=otros,
        nota=seccion("encaje_handoff").get("puntuacion") or 0,
        perfil=linkedin.perfil(contenido, fila.get("research_links")),
        busqueda=linkedin.busqueda(nombre, nombre_empresa),
        cuando=fila["created_at"],
    )


def empresas(now: datetime | None = None) -> list[Empresa]:
    """Las empresas del reporte de esta semana, ya sin repetir y en su orden. Todas: el
    tope se aplica al pintar."""
    ahora = now or datetime.now(UTC)
    dias = max(1, db_config.value("reporte_contratando_dias", DIAS))
    filas = db.fetch_all(
        _SQL_CANDIDATOS,
        {
            "desde": ahora - timedelta(days=dias),
            "evidencia": bands.EVIDENCIA_VACANTES,
            "atendidos": list(ESTADOS_ATENDIDOS),
            "no_repetir": ahora - timedelta(days=NO_REPETIR_DIAS),
        },
    )
    por_empresa: dict[str, Empresa] = {}
    for fila in filas:
        try:
            candidata = _empresa(fila)
        except Exception:
            # Un dossier raro no se lleva el reporte entero.
            logger.exception("reporte: no se pudo leer la ficha %s", fila["prospect_id"])
            continue
        if candidata is None:
            continue
        previa = por_empresa.get(candidata.clave)
        if previa is None or candidata.orden < previa.orden:
            por_empresa[candidata.clave] = candidata
    return sorted(por_empresa.values(), key=lambda e: e.orden)


def _roles(empresa: Empresa) -> str:
    """`4 open roles: Support Lead, Executive Assistant, +2 more`. Los roles que Handoff
    cubre van primero; los escribió un modelo leyendo páginas ajenas, así que se escapan
    y se recortan como en la tarjeta."""
    cuenta = _safe_number(empresa.vacantes, NUM_CHARS)
    texto = f"{cuenta} open role" + ("" if empresa.vacantes == 1 else "s")
    todos = empresa.cubiertos + empresa.otros
    nombrados = todos[:MAX_ROLES]
    if not nombrados:
        return texto
    lista = _escape_and_cap(", ".join(nombrados), ROLES_CHARS)
    # Lo que queda sin nombrar se cuenta con las vacantes, no con los roles listados:
    # el modelo no siempre lista todas las que cuenta.
    resto = max(empresa.vacantes, len(todos)) - len(nombrados)
    return f"{texto}: {lista}" + (f", +{resto} more" if resto > 0 else "")


def linea(empresa: Empresa) -> str:
    """`• *Acme* — Ada Ruiz, COO · 4 open roles: Support Lead, +3 more · LinkedIn profile
    · Open in panel`."""
    quien = ", ".join(
        b
        for b in (
            _escape_and_cap(empresa.nombre, NAME_CHARS),
            _escape_and_cap(empresa.cargo, ROLE_CHARS),
        )
        if b
    )
    partes = [
        f"*{_escape_and_cap(empresa.empresa, COMPANY_CHARS)}*" + (f" — {quien}" if quien else ""),
        _roles(empresa),
    ]
    # El perfil si se conoce; si no, una búsqueda, y el texto lo dice: nadie debe
    # comentar el post de un homónimo creyendo que el perfil estaba comprobado.
    enlace = _enlace(empresa.perfil, "LinkedIn profile") or _enlace(
        empresa.busqueda, "Find on LinkedIn (search)"
    )
    if enlace:
        partes.append(enlace)
    panel = _enlace_panel(f"/personas/{empresa.prospect_id}", "Open in panel")
    if panel:
        partes.append(panel)
    return "• " + " · ".join(partes)


def bloques(lista: list[Empresa], total: int, dias: int, nivel: str | None = None) -> list[dict]:
    """Los bloques de Slack: la cabecera con su prioridad, qué es esto en una frase, las
    líneas y un pie."""
    cuantas = f"{total} compan" + ("y" if total == 1 else "ies")
    intro = (
        f"{cuantas} with open roles seen in the last {dias} day"
        + ("" if dias == 1 else "s")
        + ". Nobody here asked for anything: this is your nurture list — open their "
        "LinkedIn, comment on a post."
    )
    lineas = [linea(e) for e in lista]
    resto = total - len(lineas)
    if resto > 0:
        lineas.append(f"_+{resto} more not shown_")
    salida = _secciones(f"{prioridad.cabecera(CABECERA, nivel)}\n{intro}", lineas)
    pie = PIE
    panel = _enlace_panel("/personas", "Open the panel")
    if panel:
        pie += f" {panel} to see everyone."
    salida.append({"type": "context", "elements": [{"type": "mrkdwn", "text": pie}]})
    return salida


def resumen_texto(total: int) -> str:
    """El texto de la notificación del móvil: cuántas, nunca cuáles."""
    return f"Weekly hiring report: {total} compan" + ("y" if total == 1 else "ies") + " hiring"


# ---------------------------------------------------------------------------------
# Publicarlo: una vez a la semana, reservando antes
# ---------------------------------------------------------------------------------


def _registrar(action: str, payload: dict) -> None:
    try:
        ledger.record_action(action, payload)
    except Exception:
        logger.exception("reporte: no se pudo registrar %s en el ledger", action)


def _parada(exc: Exception, semana: date) -> None:
    """Una parada del sistema se anota una vez por semana, no en cada pasada."""
    logger.warning("reporte semanal detenido: %s", exc)
    ya = db.fetch_one(
        "select 1 as hay from agent_actions where action = %s and payload->>'semana' = %s limit 1",
        (ACCION_PARADA, str(semana)),
    )
    if ya is None:
        _registrar(ACCION_PARADA, {"semana": str(semana), "motivo": f"{type(exc).__name__}: {exc}"})


def _decidida(semana: date) -> bool:
    """Si la semana ya no admite otro intento: salió, se cerró vacía, está saliendo
    ahora mismo, o falló demasiadas veces."""
    fila = db.fetch_one(
        "select estado, intentos from reportes_contratando where semana = %s", (semana,)
    )
    if fila is None:
        return False
    return fila["estado"] != "fallido" or fila["intentos"] >= MAX_INTENTOS


def _cerrar_vacia(semana: date) -> None:
    db.execute(
        "insert into reportes_contratando (semana, estado, intentos) values (%s, 'vacio', 0) "
        "on conflict (semana) do nothing",
        (semana,),
    )


def _reservar(semana: date) -> bool:
    """Se queda la semana ANTES de publicar. Una sola sentencia: si otro worker ya la
    tiene, o ya salió, no devuelve fila y aquí no se publica nada. Solo una semana
    `fallido` con intentos de sobra se puede volver a reservar."""
    return (
        db.fetch_one(
            """
            insert into reportes_contratando (semana, estado, intentos)
            values (%(semana)s, 'reservado', 1)
            on conflict (semana) do update
                set estado = 'reservado',
                    intentos = reportes_contratando.intentos + 1,
                    actualizado_at = now()
              where reportes_contratando.estado = 'fallido'
                and reportes_contratando.intentos < %(tope)s
            returning semana
            """,
            {"semana": semana, "tope": MAX_INTENTOS},
        )
        is not None
    )


def _toca(local: datetime) -> bool:
    """Si es el día de la semana configurado y ya es la hora. Solo ese día: un reporte
    que no pudo salir no se recupera otro día, y encender esto a media semana no
    publica nada hasta que llegue su día."""
    dia = db_config.value("reporte_contratando_dia", DIA)
    return local.isoweekday() == dia and local.hour >= db_config.value(
        "reporte_contratando_hora", HORA
    )


def _publicar(now: datetime | None) -> int:
    ahora = now or datetime.now(UTC)
    if not activo():
        return 0
    local = ahora.astimezone(_zona())
    if not _toca(local):
        return 0
    semana = local.date() - timedelta(days=local.weekday())
    if _decidida(semana):
        return 0
    try:
        guards.check_kill_switch()
        guards.check_monthly_budget()
    except (guards.KillSwitchActive, guards.MonthlyBudgetExceeded) as exc:
        _parada(exc, semana)
        return 0

    settings = load_settings()
    if not (settings.handoff_bot_token and settings.handoff_channel_id):
        # Sin bot o sin canal no hay dónde publicar; la semana no se gasta.
        logger.info("reporte semanal sin configurar (faltan HANDOFF_SLACK_*), se omite")
        return 0

    todas = empresas(ahora)
    tope = max(0, db_config.value("reporte_contratando_max", MAXIMO))
    lista = todas[:tope]
    if not lista:
        _cerrar_vacia(semana)
        return 0
    if not _reservar(semana):
        return 0

    dias = max(1, db_config.value("reporte_contratando_dias", DIAS))
    # Cuánto corre lo decide `prioridad.py`: FYI. Nadie de la lista espera nada.
    nivel = prioridad.visible(prioridad.nivel(prioridad.REPORTE_CONTRATANDO))
    try:
        canal, ts = slack_writer.post_card(
            bloques(lista, len(todas), dias, nivel),
            prioridad.resumen(resumen_texto(len(todas)), nivel),
        )
    except Exception as exc:
        logger.exception("reporte semanal: no se pudo publicar")
        try:
            db.execute(
                "update reportes_contratando set estado = 'fallido', actualizado_at = now() "
                "where semana = %s and estado = 'reservado'",
                (semana,),
            )
        except Exception:
            # Se queda `reservado`: esta semana ya no sale. Peor sería un duplicado.
            logger.exception("reporte semanal: no se pudo liberar la reserva de %s", semana)
        _registrar(
            ACCION_FALLIDO, {"semana": str(semana), "motivo": f"{type(exc).__name__}: {exc}"}
        )
        return 0

    try:
        with db.transaction() as cur:
            cur.execute(
                "update reportes_contratando set estado = 'publicado', lineas = %s, total = %s, "
                "channel_id = %s, message_ts = %s, actualizado_at = now() where semana = %s",
                (len(lista), len(todas), canal, ts, semana),
            )
            # Solo las que se pintaron: las que no cabían pueden salir la semana que
            # viene si siguen dentro de la ventana.
            for empresa in lista:
                cur.execute(
                    "insert into reportes_contratando_lineas "
                    "(prospect_id, dossier_version, semana) values (%s, %s, %s) "
                    "on conflict do nothing",
                    (empresa.prospect_id, empresa.version, semana),
                )
    except Exception:
        # Ya salió a Slack: la fila se queda `reservado` y sigue bloqueando un segundo
        # reporte esta semana, que es lo que importa.
        logger.exception("reporte semanal: publicado (ts=%s) pero no se pudo anotar", ts)
    _registrar(
        ACCION_PUBLICADO,
        {
            "semana": str(semana),
            "lineas": len(lista),
            "total": len(todas),
            "canal": canal,
            "ts": ts,
        },
    )
    return len(lista)


def publicar(now: datetime | None = None) -> int:
    """Publica el reporte de esta semana si toca. Devuelve cuántas empresas salieron.
    Nunca lanza: un reporte no puede tumbar el bucle."""
    try:
        return _publicar(now)
    except Exception:
        logger.exception("reporte semanal: fallo inesperado; se reintenta en la pasada siguiente")
        return 0
