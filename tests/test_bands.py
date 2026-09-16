import pytest

from handoff_agent import db
from handoff_agent.delivery import bands


def dossier(score):
    return {"encaje_handoff": {"puntuacion": score, "razon": "contrata soporte"}}


@pytest.mark.parametrize(
    ("score", "expected"),
    [(3, "alta"), (2, "media"), (1, "baja"), (0, None)],
)
def test_band_comes_from_the_dossier_score(conn, score, expected):
    assert bands.band_for(dossier(score)) == expected


def test_raising_alta_min_drops_the_score_to_lower_bands(conn):
    """Cambiar alta_min no elimina la puntuación, la baja de escalera."""
    db.execute("update config set value = '4'::jsonb where key = 'banda_alta_min'")
    try:
        assert bands.band_for(dossier(3)) == "media"
    finally:
        db.execute("update config set value = '3'::jsonb where key = 'banda_alta_min'")


def test_raising_media_min_makes_mid_scores_fall_to_baja(conn):
    """Cambiar media_min no elimina, solo mueve entre bandas."""
    db.execute("update config set value = '3'::jsonb where key = 'banda_media_min'")
    try:
        assert bands.band_for(dossier(2)) == "baja"
        assert bands.band_for(dossier(3)) == "alta"
    finally:
        db.execute("update config set value = '2'::jsonb where key = 'banda_media_min'")


def test_a_dossier_without_a_usable_score_is_not_delivered(conn):
    assert bands.band_for({}) is None
    assert bands.band_for({"encaje_handoff": {"puntuacion": "alta"}}) is None
