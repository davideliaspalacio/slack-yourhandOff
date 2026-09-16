"""La banda sale de la puntuación de encaje que ya trae el dossier.

El Plan 3 no construye scoring del mensaje: `encaje_handoff.puntuacion` (0 a 3)
ya la produce la síntesis, validada, y con su razón escrita. Los cortes viven
en config para poder endurecerlos en caliente si el ruido molesta. Las bandas
son una escalera: un score entra en la banda más alta que alcanza, o ninguna.
Para silenciar una puntuación entera, sube banda_baja_min por encima de ella.
"""

from __future__ import annotations

from .. import db_config


def band_for(dossier: dict) -> str | None:
    fit = dossier.get("encaje_handoff") or {}
    score = fit.get("puntuacion")
    if type(score) is not int:
        return None

    if score >= db_config.value("banda_alta_min", 3):
        return "alta"
    if score >= db_config.value("banda_media_min", 2):
        return "media"
    if score >= db_config.value("banda_baja_min", 1):
        return "baja"
    return None
