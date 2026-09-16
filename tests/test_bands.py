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


def test_thresholds_can_be_moved_without_a_redeploy(conn):
    """El modo de fallo real es el ruido: el umbral tiene que poder subirse en
    caliente, sin tocar el código."""
    db.execute("update config set value = '3'::jsonb where key = 'banda_media_min'")
    assert bands.band_for(dossier(2)) is None
    assert bands.band_for(dossier(3)) == "alta"


def test_a_dossier_without_a_usable_score_is_not_delivered(conn):
    assert bands.band_for({}) is None
    assert bands.band_for({"encaje_handoff": {"puntuacion": "alta"}}) is None
