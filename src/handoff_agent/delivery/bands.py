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
"""

from __future__ import annotations

from .. import db_config
from ..dossier import SIN_EVIDENCIA


def band_for(dossier: dict) -> str | None:
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
