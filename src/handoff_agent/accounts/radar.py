"""Radar de cuentas objetivo (spec 2026-09-22, §5).

Cada cuenta vigilada se escanea una vez cada `radar_horas_entre_escaneos`: se
leen sus vacantes abiertas con JobSpy (superficie pública, sin cuenta), se
guardan como señales una por rol, se cierran las que llevan días sin verse y
se puntúan por código. Una señal fuerte (`radar_auto_min`) pasa sola a
`pursued`, que es donde la recoge la fase 2 (decisor).

Unipile solo entra para resolver, una vez, el id de LinkedIn de la empresa. Si
sus guardarraíles dicen que no (horario, tope, pausa, sin claves), el radar
sigue por nombre: no es un error de escaneo.

Un fallo en una cuenta se anota en su `last_scan_error` y el radar sigue con
la siguiente. Solo el kill switch para el radar entero.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import asdict, dataclass

from .. import db, db_config, guards, ledger
from ..tools import jobs, unipile
from ..tools.jobs import _normalise_company

logger = logging.getLogger(__name__)

ACCION_ESCANEO = "radar_escaneo"
ACCION_RESOLVER = "radar_resolver_empresa"
LIMITE_POR_CICLO = 5
_MAX_URLS = 10
_MAX_URL_CHARS = 500
_MAX_TITULO = 300
_MAX_ERROR = 500
ROLES_IGNORADOS_POR_DEFECTO = ["intern", "internship", "práctica", "becario"]

# Puntuación (spec §5): base de toda vacante nueva y lo que suma cada señal.
SCORE_BASE = 3
SCORE_ANTIGUA = 2
SCORE_REPUBLICADA = 2
SCORE_MUCHAS_ABIERTAS = 2
SCORE_VARIAS_FUENTES = 1
SCORE_MAX = 10
DIAS_ANTIGUA = 30
ABIERTAS_PARA_SUMAR = 3


@dataclass
class ResultadoEscaneo:
    account_id: str
    nombre: str
    vistas: int = 0
    nuevas: int = 0
    reabiertas: int = 0
    cerradas: int = 0
    ignoradas: int = 0
    auto_pursued: int = 0
    linkedin_company_id: str | None = None
    error: str | None = None


# --- funciones puras ------------------------------------------------------------

_GENERO_RE = re.compile(
    r"[\(\[]\s*(?:[a-z]{1,3}\s*/\s*)+[a-z]{1,3}\s*[\)\]]|[\(\[]\s*all genders?\s*[\)\]]"
)
_MODALIDAD = r"(?:remote|remoto|remota|hybrid|hibrido|hibrida|on-?site|presencial|teletrabajo)"
_MODALIDAD_RE = re.compile(
    rf"(?:\s*[-–—|,:/]\s*[\(\[]?\s*{_MODALIDAD}\s*[\)\]]?|\s*[\(\[]\s*{_MODALIDAD}\s*[\)\]])\s*$"
)
_SEPARADORES_FINALES = " -–—|,:/"


def _sin_acentos(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def normalizar_titulo(titulo: str) -> str:
    """La clave de una vacante: dos anuncios con la misma clave son el mismo rol.

    Minúsculas, sin acentos, sin "(m/f/d)" ni "(all genders)", sin la
    modalidad al final ("- Remote", "(Hybrid)", "| Remoto") y con los
    espacios colapsados.
    """
    clave = _sin_acentos(titulo).casefold()
    clave = _GENERO_RE.sub(" ", clave)
    clave = " ".join(clave.split())
    anterior = None
    while anterior != clave:
        anterior = clave
        clave = _MODALIDAD_RE.sub("", clave).strip(_SEPARADORES_FINALES)
    return " ".join(clave.split())


def es_rol_ignorado(title_key: str, roles_ignorados: list[str]) -> bool:
    """True si la clave contiene, como palabra entera, alguno de los roles
    ignorados ("intern" sí casa con "summer intern", no con "internal")."""
    for rol in roles_ignorados:
        if not isinstance(rol, str):
            continue
        termino = normalizar_titulo(rol)
        if termino and re.search(rf"(?<!\w){re.escape(termino)}(?!\w)", title_key):
            return True
    return False


def calcular_score(
    *, dias_abierta: int, reposted: bool, abiertas_cuenta_30d: int, num_fuentes: int
) -> int:
    """Puntuación 0-10 de una vacante (spec §5), sin LLM: se explica sola."""
    score = SCORE_BASE
    if dias_abierta >= DIAS_ANTIGUA:
        score += SCORE_ANTIGUA
    if reposted:
        score += SCORE_REPUBLICADA
    if abiertas_cuenta_30d >= ABIERTAS_PARA_SUMAR:
        score += SCORE_MUCHAS_ABIERTAS
    if num_fuentes >= 2:
        score += SCORE_VARIAS_FUENTES
    return min(score, SCORE_MAX)


def _clave_empresa(nombre: str) -> str:
    return _normalise_company(_sin_acentos(nombre))


def elegir_empresa(nombre: str, candidatos: list[tuple[str, str]]) -> tuple[str, str] | None:
    """El candidato de LinkedIn cuyo nombre normalizado coincide con el de la
    cuenta. También vale la parte antes de " – ", " | " o "(": LinkedIn
    llama "CODELCO – Corporación Nacional del Cobre" a Codelco. Sin
    coincidencia exacta, ninguno: mejor sin id que con el de otra empresa."""
    buscada = _clave_empresa(nombre)
    if not buscada:
        return None
    for empresa_id, titulo in candidatos:
        variantes = {titulo, re.split(r"\s+[-–—|]\s+|\s*\(", titulo, maxsplit=1)[0]}
        if any(_clave_empresa(v) == buscada for v in variantes):
            return empresa_id, titulo
    return None


# --- escaneo --------------------------------------------------------------------


def _resolver_linkedin_id(cuenta: dict) -> str | None:
    """Resuelve el id de LinkedIn de la cuenta, una sola vez.

    "Una vez" quiere decir una respuesta de Unipile: un guardarraíl (horario,
    tope, pausa, sin claves) no gasta el intento, y la cuenta se sigue
    escaneando por nombre mientras tanto. Si Unipile contestó y ninguna
    empresa coincide, no se vuelve a preguntar: el id se corrige en el panel.
    """
    ya_intentado = db.fetch_one(
        "select 1 from agent_actions where action = %s and payload->>'account_id' = %s limit 1",
        (ACCION_RESOLVER, str(cuenta["id"])),
    )
    if ya_intentado:
        return None
    try:
        candidatos = unipile.resolver_empresa(cuenta["name"])
    except unipile.UnipileError as exc:
        logger.info("radar: sin id de LinkedIn para %s: %s", cuenta["name"], exc)
        return None

    elegido = elegir_empresa(cuenta["name"], candidatos)
    ledger.record_action(
        ACCION_RESOLVER,
        {"account_id": str(cuenta["id"]), "nombre": cuenta["name"]},
        {
            "resuelto": elegido is not None,
            "linkedin_company_id": elegido[0] if elegido else None,
            "candidatos": len(candidatos),
        },
    )
    if elegido is None:
        return None
    db.execute(
        "update target_accounts set linkedin_company_id = %s, linkedin_name = %s, "
        "updated_at = now() where id = %s",
        (elegido[0], elegido[1][:_MAX_TITULO], cuenta["id"]),
    )
    return elegido[0]


def _agrupar(postings: list[jobs.JobPosting], roles_ignorados: list[str], resultado) -> dict:
    """Una entrada por clave de título, con las fuentes y URLs de todos sus anuncios."""
    agrupadas: dict[str, dict] = {}
    for posting in postings:
        titulo = " ".join(posting.title.split())[:_MAX_TITULO]
        clave = normalizar_titulo(titulo)
        if not clave:
            continue
        if es_rol_ignorado(clave, roles_ignorados):
            resultado.ignoradas += 1
            continue
        entrada = agrupadas.setdefault(
            clave, {"title": titulo, "location": None, "sources": set(), "urls": []}
        )
        entrada["sources"].add(posting.site)
        location = posting.location.strip()
        if location and location != "nan" and not entrada["location"]:
            entrada["location"] = location[:_MAX_TITULO]
        url = posting.url.strip()
        if (
            url.startswith(("https://", "http://"))
            and len(url) <= _MAX_URL_CHARS
            and url not in entrada["urls"]
        ):
            entrada["urls"].append(url)
    return agrupadas


def _guardar_senales(account_id, agrupadas: dict, resultado: ResultadoEscaneo) -> None:
    cerradas_antes = {
        row["title_key"]
        for row in db.fetch_all(
            "select title_key from hiring_signals where account_id = %s and closed_at is not null",
            (account_id,),
        )
    }
    existentes = {
        row["title_key"]
        for row in db.fetch_all(
            "select title_key from hiring_signals where account_id = %s", (account_id,)
        )
    }
    for clave, entrada in agrupadas.items():
        resultado.vistas += 1
        if clave not in existentes:
            resultado.nuevas += 1
        elif clave in cerradas_antes:
            resultado.reabiertas += 1
        # Upsert y no select + insert: dos workers escaneando la misma cuenta
        # a la vez no pueden duplicar una vacante. En el DO UPDATE,
        # hiring_signals.* es la fila que ya estaba: una cerrada que reaparece
        # es una re-publicación.
        db.execute(
            """
            insert into hiring_signals (account_id, title, title_key, location, sources, urls)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (account_id, title_key) do update set
                last_seen_at = now(),
                location     = coalesce(hiring_signals.location, excluded.location),
                sources      = array(select distinct s
                                     from unnest(hiring_signals.sources || excluded.sources) s
                                     order by 1),
                urls         = (array(select distinct u
                                      from unnest(hiring_signals.urls || excluded.urls) u
                                      order by 1))[1:%s],
                reposted     = hiring_signals.reposted or hiring_signals.closed_at is not null,
                closed_at    = null,
                updated_at   = now()
            """,
            (
                account_id,
                entrada["title"],
                clave,
                entrada["location"],
                sorted(entrada["sources"]),
                entrada["urls"][:_MAX_URLS],
                _MAX_URLS,
            ),
        )


def _cerrar_no_vistas(account_id) -> int:
    dias = db_config.value("radar_dias_para_cerrar", 3)
    return db.execute(
        "update hiring_signals set closed_at = now(), updated_at = now() "
        "where account_id = %s and closed_at is null "
        "and last_seen_at < now() - %s * interval '1 day'",
        (account_id, dias),
    )


def _puntuar(account_id) -> None:
    abiertas_30d = db.fetch_one(
        "select count(*) as n from hiring_signals "
        "where account_id = %s and last_seen_at >= now() - interval '30 days'",
        (account_id,),
    )["n"]
    for senal in db.fetch_all(
        "select id, score, reposted, cardinality(sources) as fuentes, "
        "extract(day from now() - first_seen_at)::int as dias "
        "from hiring_signals where account_id = %s and closed_at is null",
        (account_id,),
    ):
        score = calcular_score(
            dias_abierta=senal["dias"],
            reposted=senal["reposted"],
            abiertas_cuenta_30d=abiertas_30d,
            num_fuentes=senal["fuentes"],
        )
        if score != senal["score"]:
            db.execute(
                "update hiring_signals set score = %s, updated_at = now() where id = %s",
                (score, senal["id"]),
            )


def _auto_pursue(account_id) -> int:
    minimo = db_config.value("radar_auto_min", 7)
    return db.execute(
        "update hiring_signals set status = 'pursued', updated_at = now() "
        "where account_id = %s and status = 'new' and closed_at is null and score >= %s",
        (account_id, minimo),
    )


def _escanear(cuenta: dict) -> ResultadoEscaneo:
    resultado = ResultadoEscaneo(account_id=str(cuenta["id"]), nombre=cuenta["name"])
    linkedin_id = cuenta.get("linkedin_company_id") or _resolver_linkedin_id(cuenta)
    resultado.linkedin_company_id = linkedin_id

    ofertas = jobs.ofertas_de_cuenta(cuenta["name"], linkedin_company_id=linkedin_id)
    roles_ignorados = db_config.value("radar_roles_ignorados", ROLES_IGNORADOS_POR_DEFECTO)
    agrupadas = _agrupar(ofertas.postings, roles_ignorados, resultado)
    _guardar_senales(cuenta["id"], agrupadas, resultado)

    # Con un board caído, no ver una vacante no dice que se haya cerrado.
    if not ofertas.errores:
        resultado.cerradas = _cerrar_no_vistas(cuenta["id"])
    _puntuar(cuenta["id"])
    resultado.auto_pursued = _auto_pursue(cuenta["id"])
    if ofertas.errores:
        resultado.error = "; ".join(ofertas.errores)[:_MAX_ERROR]
    return resultado


def escanear_cuenta(cuenta: dict) -> ResultadoEscaneo:
    """Escanea una cuenta y deja constancia en ella y en el ledger. Nunca
    lanza salvo por el kill switch: cualquier otro fallo va a last_scan_error."""
    try:
        resultado = _escanear(cuenta)
    except guards.KillSwitchActive:
        raise
    except Exception as exc:  # una cuenta rota no para el radar
        logger.exception("radar: fallo escaneando %s", cuenta["name"])
        resultado = ResultadoEscaneo(
            account_id=str(cuenta["id"]),
            nombre=cuenta["name"],
            error=f"{type(exc).__name__}: {exc}"[:_MAX_ERROR],
        )

    db.execute(
        "update target_accounts set last_scan_at = now(), last_scan_error = %s, "
        "updated_at = now() where id = %s",
        (resultado.error, cuenta["id"]),
    )
    detalle = asdict(resultado)
    ledger.record_action(
        ACCION_ESCANEO,
        {"account_id": detalle.pop("account_id"), "nombre": detalle.pop("nombre")},
        detalle,
    )
    return resultado


def _reactivar_pospuestas() -> int:
    """Las señales pospuestas cuyo plazo venció vuelven a la bandeja."""
    return db.execute(
        "update hiring_signals set status = 'new', snoozed_until = null, updated_at = now() "
        "where status = 'snoozed' and snoozed_until <= now()"
    )


def escanear_pendientes(
    forzar: bool = False, limite: int = LIMITE_POR_CICLO
) -> list[ResultadoEscaneo]:
    """Escanea las cuentas vigiladas a las que les toca, hasta `limite`.

    Las cuentas se reclaman marcando last_scan_at antes de escanearlas (con
    SKIP LOCKED): dos workers no escanean la misma, y una cuenta que falla no
    se reintenta en cada vuelta del bucle. El tope por llamada evita que un
    ciclo del worker se quede minutos en JobSpy con la cola de research parada.
    """
    guards.check_kill_switch()
    _reactivar_pospuestas()
    horas = db_config.value("radar_horas_entre_escaneos", 20)
    cuentas = db.fetch_all(
        """
        update target_accounts set last_scan_at = now()
        where id in (
            select id from target_accounts
            where status = 'watching'
              and (%s or last_scan_at is null or last_scan_at < now() - %s * interval '1 hour')
            order by last_scan_at nulls first, created_at
            limit %s
            for update skip locked
        )
        returning *
        """,
        (forzar, horas, limite),
    )
    cuentas.sort(key=lambda c: c["created_at"])
    return [escanear_cuenta(cuenta) for cuenta in cuentas]


def escanear_por_id(account_id: str) -> ResultadoEscaneo:
    """Escanea una cuenta ya, le toque o no (y aunque esté en pausa)."""
    guards.check_kill_switch()
    cuenta = db.fetch_one("select * from target_accounts where id = %s", (str(account_id),))
    if cuenta is None:
        raise LookupError(f"no existe la cuenta {account_id}")
    return escanear_cuenta(cuenta)
