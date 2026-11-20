"""La banda sale de la puntuación de encaje que ya trae el dossier.

El Plan 3 no construye scoring del mensaje: `encaje_handoff.puntuacion` (0 a 3)
ya la produce la síntesis, validada, y con su razón escrita. Los cortes viven
en config para poder endurecerlos en caliente si el ruido molesta. Las bandas
son una escalera: un score entra en la banda más alta que alcanza, o ninguna.
Para silenciar una puntuación entera, sube banda_baja_min por encima de ella.

La puntuación sola no basta para interrumpir: la síntesis declara además de
dónde sale (`encaje_handoff.evidencia`), y una nota sin prueba se queda en
banda baja por alta que sea. El prompt ya lo pide, pero un prompt no es un
candado: este sí.

Y una prueba que sí existe puede no ser accionable. Con evidencia "vacantes" lo único
que se sabe es que la empresa publicó ofertas: la persona no dijo ni pidió nada. AG lo
dijo así: "I can't do anything", "I don't even need the alert every day". Esas fichas
no interrumpen, tengan la nota que tengan: se quedan en banda baja (sin tarjeta ni SMS)
y salen juntas, una vez a la semana, en el reporte de empresas contratando
(`reporte_contratando.py`). La regla entera vive aquí, en `solo_vacantes` y
`va_al_reporte`: el resto del sistema pregunta la banda y no vuelve a decidirlo.
"""

from __future__ import annotations

from .. import db_config
from ..dossier import SIN_EVIDENCIA

# El valor de `encaje_handoff.evidencia` (lista cerrada en `dossier.EVIDENCIAS`) que
# dice "hay vacantes publicadas" y nada más. Los otros tres no entran en la regla:
# "necesidad_declarada" es algo que la persona pidió con sus palabras,
# "equipo_relevante" nunca fue "tiene vacantes", y "ninguna" ya lo frena
# `exigir_evidencia`.
EVIDENCIA_VACANTES = "vacantes"
# Las personas del radar de cuentas objetivo (`li:<id>`, ver ingest/queue.py). Se
# repite aquí el prefijo en vez de importarlo: delivery no depende de ingest.
PREFIJO_RADAR = "li:"


def solo_vacantes(dossier: dict) -> bool:
    """Si la única evidencia del encaje son vacantes publicadas.

    Es una comparación con un campo que la síntesis ya rellena y la validación ya
    exige (`dossier.validate_dossier`): ni modelo ni heurística. Si la persona además
    pidió algo, la síntesis declara "necesidad_declarada" y esto es False."""
    fit = dossier.get("encaje_handoff") if isinstance(dossier, dict) else None
    return isinstance(fit, dict) and fit.get("evidencia") == EVIDENCIA_VACANTES


def del_radar(slack_user_id: object) -> bool:
    return str(slack_user_id or "").startswith(PREFIJO_RADAR)


def va_al_reporte(dossier: dict, slack_user_id: object = None) -> bool:
    """Si esta ficha sale en el reporte semanal en vez de en una tarjeta.

    Dos excepciones a `solo_vacantes`:

    - `vacantes_solo_reporte` (0040, Settings) apagado: todo vuelve a ser como antes.
    - Las personas del radar de cuentas objetivo: ahí la vacante no es un hallazgo del
      research sino el motivo por el que alguien de Handoff decidió perseguir esa
      cuenta y buscar a quien decide. Su tarjeta es el resultado de ese encargo, y el
      reporte semanal es de miembros del club.
    """
    return (
        solo_vacantes(dossier)
        and not del_radar(slack_user_id)
        and db_config.value("vacantes_solo_reporte", True)
    )


def banda_por_nota(dossier: dict) -> str | None:
    """La banda que da la nota con su evidencia, sin mirar si es "solo vacantes".

    Es `band_for` de antes de 0040. La usa el reporte semanal para quedarse con las
    fichas que habrían tenido tarjeta: una empresa con vacantes que Handoff no cubre
    (nota baja) no es una empresa a la que hacer seguimiento."""
    fit = dossier.get("encaje_handoff") or {}
    score = fit.get("puntuacion")
    if type(score) is not int:
        return None

    if score >= db_config.value("banda_alta_min", 3):
        band = "alta"
    elif score >= db_config.value("banda_media_min", 2):
        band = "media"
    elif score >= db_config.value("banda_baja_min", 1):
        band = "baja"
    else:
        return None

    # `encaje_handoff.evidencia` == "ninguna": el propio modelo dice que la
    # nota no se apoya en vacantes, ni en una necesidad pedida, ni en un equipo
    # real. Publicamos una tarjeta 2/3 cuya razón era "likely has needs...
    # no direct evidence of current open roles"; con esto se queda en "baja"
    # (panel y email diario, sin tarjeta ni SMS). Solo actúa ante un "ninguna"
    # explícito: las fichas guardadas antes de que existiera el campo no lo
    # traen y siguen como estaban. `exigir_evidencia` (0020) lo apaga en
    # caliente desde Settings.
    if (
        band in ("alta", "media")
        and fit.get("evidencia") == SIN_EVIDENCIA
        and db_config.value("exigir_evidencia", True)
    ):
        return "baja"

    # `empresa_no_confirmada` (research/worker.py, a partir de
    # Gathered.unconfirmed_domain): la web se adivinó y no se pudo confirmar,
    # así que el dossier puede describir la empresa equivocada. Una "alta" baja
    # a "media" para no disparar el SMS con esa incertidumbre; el resto de
    # bandas se queda igual, ya no llevan SMS.
    if band == "alta" and dossier.get("empresa_no_confirmada"):
        return "media"
    return band


def band_for(dossier: dict, slack_user_id: object = None) -> str | None:
    """La banda de un dossier: la que decide si interrumpe.

    `slack_user_id` es el de la persona, si se sabe: solo sirve para reconocer a las
    del radar (ver `va_al_reporte`). Sin él, la ficha se trata como la de un miembro
    del club, que es lo que son casi todas."""
    band = banda_por_nota(dossier)
    # "Solo vacantes" no interrumpe, sea cual sea la nota: banda baja, igual que una
    # nota sin prueba. La recoge el reporte semanal.
    if band in ("alta", "media") and va_al_reporte(dossier, slack_user_id):
        return "baja"
    return band
