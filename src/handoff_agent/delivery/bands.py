"""La banda sale de la puntuación de encaje que ya trae el dossier.

El Plan 3 no construye scoring del mensaje: `encaje_handoff.puntuacion` (0 a 3)
ya la produce la síntesis, validada, y con su razón escrita. Los cortes viven
en config para poder endurecerlos en caliente si el ruido molesta.
"""

from __future__ import annotations

from .. import db_config


def band_for(dossier: dict) -> str | None:
    fit = dossier.get("encaje_handoff") or {}
    score = fit.get("puntuacion")
    if type(score) is not int:
        return None

    alta_min = db_config.value("banda_alta_min", 3)
    media_min = db_config.value("banda_media_min", 2)
    baja_min = db_config.value("banda_baja_min", 1)

    # Si el umbral media sube demasiado (>= alta), dejamos de entregar media y
    # baja: solo alertas alta o nada.
    if media_min >= alta_min:
        if score >= alta_min:
            return "alta"
        else:
            return None

    if score >= alta_min:
        return "alta"
    if score >= media_min:
        return "media"
    if score >= baja_min:
        return "baja"
    return None
