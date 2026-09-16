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


def test_the_ladder_ignores_thresholds_when_inverted(conn):
    """Cuando media_min >= alta_min, la escalera sigue funcionando.

    Esta configuración es inválida operativamente, pero el código debe
    ignorarla sin devolver None: si hay una puntuación, se intenta entregarla.

    Esto prueba que la escalera NO tiene el caso especial removido en e227cea:
        if media_min >= alta_min:
            if score >= alta_min:
                return "alta"
            else:
                return None

    Con media_min=4, alta_min=2, score=1:
    - Código buggy: 4 >= 2 → true, 1 >= 2 → false → return None ❌
    - Código correcto: 1 >= 2 → false, 1 >= 4 → false, 1 >= 1 → true → "baja" ✓
    """
    db.execute("update config set value = '4'::jsonb where key = 'banda_media_min'")
    db.execute("update config set value = '2'::jsonb where key = 'banda_alta_min'")
    try:
        assert bands.band_for(dossier(1)) == "baja"
    finally:
        db.execute("update config set value = '2'::jsonb where key = 'banda_media_min'")
        db.execute("update config set value = '3'::jsonb where key = 'banda_alta_min'")


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


def test_silencing_a_score_by_raising_baja_min(conn):
    """Para silenciar una puntuación entera, sube banda_baja_min por encima.

    Con banda_baja_min = 2, un score de 1 devuelve None en lugar de "baja".
    """
    db.execute("update config set value = '2'::jsonb where key = 'banda_baja_min'")
    try:
        assert bands.band_for(dossier(1)) is None
    finally:
        db.execute("update config set value = '1'::jsonb where key = 'banda_baja_min'")


def test_silencing_works_but_a_higher_score_still_delivers(conn):
    """Silenciar un score no silencia los superiores."""
    db.execute("update config set value = '2'::jsonb where key = 'banda_baja_min'")
    try:
        assert bands.band_for(dossier(1)) is None
        assert bands.band_for(dossier(2)) == "media"
    finally:
        db.execute("update config set value = '1'::jsonb where key = 'banda_baja_min'")


def test_a_float_score_is_rejected(conn):
    """Solo aceptamos int, no float."""
    assert bands.band_for(dossier(3.5)) is None


def test_a_true_score_is_rejected(conn):
    """bool es subclase de int en Python, pero rechazamos bool explícitamente.

    type(True) is not int → True, así que devuelve None.
    """
    assert bands.band_for(dossier(True)) is None


def test_a_dossier_without_a_usable_score_is_not_delivered(conn):
    assert bands.band_for({}) is None
    assert bands.band_for({"encaje_handoff": {"puntuacion": "alta"}}) is None
