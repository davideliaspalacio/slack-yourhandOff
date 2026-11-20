"""Qué atender primero: el nivel de prioridad de cada aviso (migración 0039).

AG lo pidió (punto 7): "priorizar notificaciones para organizarme mejor". Todos los
avisos caen en un mismo canal y cada uno tenía su cabecera, pero ninguna decía cuánto
corría. Aquí -- y solo aquí -- se decide el nivel de cada tipo de aviso. Quien publica
(`deliver.py`, `signals/alertas.py`, `signals/eventos.py`, `signals/miembros.py`,
`seguimiento.py`, `aviso_lector.py`, `novedades.py`) y el resumen del día
(`resumen_prioridad.py`) preguntan aquí y pintan lo que salga; ninguno decide por su
cuenta.

Tres niveles, y qué significa cada uno para alguien que no es técnico:

- **ACT NOW** (`AHORA`): hay un posible cliente esperando, o el agente está ciego.
- **TODAY** (`HOY`): merece unos minutos antes de acabar el día; mañana ya es tarde.
- **FYI** (`FYI`): para estar al tanto. Nadie espera nada de él.

Todo es por reglas, sin modelo: la misma entrada da siempre el mismo nivel, y la tabla
de `nivel` se puede leer entera de una vez. Lo que cambia el nivel dentro de un mismo
tipo son datos que el sistema ya tenía:

- la **banda** del dossier (`bands.py`): alta o media;
- la **confianza** con la que el detector vio la necesidad (`signals/detector.py`);
- si quien espera es un **lead caliente** (`es_caliente`): alguien con encaje alto o
  medio, o con quien ya se habló (`contactado`, `cliente`).

Esto es orden y presentación: aquí no se decide si un aviso sale ni cuándo, y ningún
tope cambia. Por eso nada de este módulo puede tumbar un aviso: ante un fallo se
contesta "no es caliente" o "sin marca", y el aviso sale como salía antes.

La marca va delante de la cabecera de cada aviso, igual en todos
("🔴 *ACT NOW* · 🔥 *HIGH SIGNAL* · fit 3/3"), y delante del texto de la notificación
del móvil ("🔴 Act now · High signal: Ada Ruiz"). Lo que distingue a cada tipo -- su
emoji y su título -- sigue intacto justo detrás. `prioridad_activa` (Settings) la apaga.

El panel ordena la pantalla Today con estas mismas reglas (`panel/lib/prioridad.ts`,
que no puede importar Python: un test vigila que las etiquetas no se separen).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from .. import db, db_config
from . import bands

logger = logging.getLogger(__name__)

AHORA = "ahora"
HOY = "hoy"
FYI = "fyi"
# De lo más urgente a lo menos: el orden de esta tupla es el orden de todo lo demás.
NIVELES = (AHORA, HOY, FYI)

ETIQUETAS = {AHORA: "ACT NOW", HOY: "TODAY", FYI: "FYI"}
# En la notificación del móvil no hay negrita: en minúsculas se lee mejor que a gritos.
ETIQUETAS_RESUMEN = {AHORA: "Act now", HOY: "Today", FYI: "FYI"}
EMOJI = {AHORA: "🔴", HOY: "🟡", FYI: "⚪"}
# Una línea por nivel, para el resumen del día: qué quiere decir, sin jerga.
EXPLICACION = {
    AHORA: "a likely customer is waiting",
    HOY: "worth a few minutes before the day ends",
    FYI: "good to know, nothing waiting on you",
}

# Los tipos de aviso. Los de seguimiento usan los mismos valores que
# `seguimiento.py`/`seguimiento_canal.py` (un test vigila que no se separen), y
# `MENCION` vale igual para la mención recién hecha (`signals/eventos.py`) que para la
# que sigue sin contestar: la regla es la misma.
RESEARCH = "research"
PIDIERON = "pidieron"
MENCION = "mencion"
PRESENTACION = "presentacion"
MIEMBRO_NUEVO = "miembro_nuevo"
NO_LEIDO = "no_leido"
RESPONDER = "responder"
HILO = "hilo"
SIN_FICHA = "responder_sin_ficha"
SIN_RESPUESTA = "sin_respuesta"
SIN_CONVERSACION = "sin_conversacion"
CREDENCIAL = "credencial_caducada"
NOVEDADES = "novedades"

TIPOS = (
    RESEARCH,
    PIDIERON,
    MENCION,
    PRESENTACION,
    MIEMBRO_NUEVO,
    NO_LEIDO,
    RESPONDER,
    HILO,
    SIN_FICHA,
    SIN_RESPUESTA,
    SIN_CONVERSACION,
    CREDENCIAL,
    NOVEDADES,
)

BANDAS_CON_TARJETA = ("alta", "media")
# Con quien ya se habló: si esa persona le escribe o le menciona, no es un desconocido.
ESTADOS_CALIENTES = ("contactado", "cliente")


def nivel(
    tipo: str,
    *,
    banda: str | None = None,
    confianza: str | None = None,
    caliente: bool = False,
) -> str:
    """El nivel de un aviso. La tabla entera, de arriba abajo:

    | Aviso                                          | Nivel                               |
    |------------------------------------------------|-------------------------------------|
    | Tarjeta de research                            | banda alta: AHORA · media: HOY      |
    | "They asked" (señal de compra)                 | confianza alta: AHORA · resto: HOY  |
    | Mención (recién hecha o sin contestar)         | lead caliente: AHORA · resto: HOY   |
    | Respuesta en un hilo sin contestar             | lead caliente: AHORA · resto: HOY   |
    | DM de un lead sin leer o sin responder         | lead caliente: AHORA · resto: HOY   |
    | DM de alguien sin ficha                        | HOY                                 |
    | Presentación ("say hi")                        | HOY                                 |
    | Miembro nuevo                                  | encaje alto o medio: HOY · resto: FYI |
    | Él escribió y no le contestan                  | FYI                                 |
    | Marcado contactado, sin conversación           | FYI                                 |
    | Credencial del lector caducada                 | AHORA                               |
    | "What's new"                                   | FYI                                 |

    Un tipo que no esté aquí es FYI: lo desconocido nunca interrumpe por su cuenta.
    """
    if tipo == RESEARCH:
        # La señal alta es la única que además manda SMS: es lo más caliente que hay.
        if banda == "alta":
            return AHORA
        return HOY if banda == "media" else FYI
    if tipo == PIDIERON:
        # Alguien dijo con sus palabras que necesita lo que Handoff vende. Con confianza
        # alta la necesidad es inequívoca; con menos, hay que leerla antes de correr.
        return AHORA if confianza == "alta" else HOY
    if tipo in (MENCION, HILO, NO_LEIDO, RESPONDER):
        # Alguien espera algo de él. Que corra depende de quién sea.
        return AHORA if caliente else HOY
    if tipo in (SIN_FICHA, PRESENTACION):
        # Un saludo o una respuesta de cortesía valen hoy; pasado mañana ya no.
        return HOY
    if tipo == MIEMBRO_NUEVO:
        return HOY if banda in BANDAS_CON_TARJETA else FYI
    if tipo == CREDENCIAL:
        # Sin credencial el agente no lee nada: todo lo demás se para hasta que se pegue.
        return AHORA
    # SIN_RESPUESTA, SIN_CONVERSACION, NOVEDADES y cualquier tipo desconocido: la pelota
    # no está en su tejado.
    return FYI


def orden(valor: str | None) -> int:
    """La posición de un nivel, del más urgente (0) al menos. Lo desconocido, al final."""
    return NIVELES.index(valor) if valor in NIVELES else len(NIVELES)


def mas_urgente(valores: Iterable[str | None]) -> str | None:
    """El nivel de un aviso que junta varios casos: el del caso que más corre."""
    conocidos = [v for v in valores if v in NIVELES]
    return min(conocidos, key=orden) if conocidos else None


def activa() -> bool:
    """El interruptor de Settings. Encendido salvo que la base diga `false`; si la base
    no contesta, apagado: sin marca el aviso es exactamente el de antes."""
    try:
        return db_config.value("prioridad_activa", True) is True
    except Exception:
        logger.exception("prioridad: no se pudo leer el interruptor; los avisos salen sin marca")
        return False


def visible(valor: str | None) -> str | None:
    """El nivel tal como debe pintarse: el mismo, o None si las marcas están apagadas."""
    return valor if valor in NIVELES and activa() else None


def marca(valor: str | None) -> str:
    """`🔴 *ACT NOW*`, o cadena vacía si no hay nivel que pintar."""
    return f"{EMOJI[valor]} *{ETIQUETAS[valor]}*" if valor in NIVELES else ""


def cabecera(texto: str, valor: str | None) -> str:
    """La cabecera de un aviso con su nivel delante. Sin nivel, la cabecera de siempre.

    Todo lo que se añade es texto fijo de este módulo (como mucho 15 caracteres más el
    separador): no puede romper el `mrkdwn` ni acercar un bloque a su tope."""
    return f"{marca(valor)} · {texto}" if valor in NIVELES else texto


def resumen(texto: str, valor: str | None) -> str:
    """El texto de la notificación del móvil con su nivel delante."""
    if valor not in NIVELES:
        return texto
    return f"{EMOJI[valor]} {ETIQUETAS_RESUMEN[valor]} · {texto}"


# Slack puede devolver un emoji como lo mandamos o por su nombre entre dos puntos.
_MARCA = re.compile(
    r"^(?:🔴|🟡|⚪|:red_circle:|:large_yellow_circle:|:white_circle:) \*(?:"
    + "|".join(re.escape(e) for e in ETIQUETAS.values())
    + r")\* · "
)


def sin_marca(texto: str) -> str:
    """La cabecera sin su nivel. Lo usa el receptor de los botones (`web/app.py`): una
    tarjeta ya marcada "Contacted" o "Discard" no puede seguir diciendo ACT NOW."""
    return _MARCA.sub("", texto, count=1)


def es_caliente(prospect_id: str | None) -> bool:
    """Si esta persona es un lead caliente: su último dossier da banda alta o media, o
    ya se habló con ella (`contactado`, `cliente`).

    Es lo que separa "te escribió alguien" de "te escribió alguien a quien quieres
    venderle". Sin ficha, o si la base falla, no lo es: la prioridad nunca puede costar
    un aviso, así que ante la duda el aviso sale con el nivel más bajo de su tipo."""
    if not prospect_id:
        return False
    try:
        fila = db.fetch_one(
            """
            select p.state::text as state,
                   (select d.content from dossiers d
                     where d.prospect_id = p.id order by d.version desc limit 1) as content
              from prospects p
             where p.id = %s
            """,
            (prospect_id,),
        )
        if fila is None:
            return False
        if fila["state"] in ESTADOS_CALIENTES:
            return True
        contenido = fila["content"]
        return isinstance(contenido, dict) and bands.band_for(contenido) in BANDAS_CON_TARJETA
    except Exception:
        logger.exception("prioridad: no se pudo saber si %s es un lead caliente", prospect_id)
        return False
