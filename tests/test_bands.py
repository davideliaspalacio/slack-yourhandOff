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


# --- empresa_no_confirmada baja "alta" a "media": sin SMS con empresa dudosa -


def test_an_unconfirmed_company_downgrades_alta_to_media(conn):
    payload = {**dossier(3), "empresa_no_confirmada": {"dominio_adivinado": "handoff.ai"}}
    assert bands.band_for(payload) == "media"


def test_an_unconfirmed_company_does_not_change_media_or_baja(conn):
    media = {**dossier(2), "empresa_no_confirmada": {"dominio_adivinado": "handoff.ai"}}
    baja = {**dossier(1), "empresa_no_confirmada": {"dominio_adivinado": "handoff.ai"}}
    assert bands.band_for(media) == "media"
    assert bands.band_for(baja) == "baja"


def test_a_confirmed_company_keeps_alta(conn):
    assert bands.band_for(dossier(3)) == "alta"


# --- evidencia "ninguna": una nota sin pruebas no pasa de "baja" -------------


def con_evidencia(score, evidencia):
    return {"encaje_handoff": {"puntuacion": score, "evidencia": evidencia, "razon": "r"}}


@pytest.mark.parametrize("score", [2, 3])
def test_a_score_without_evidence_is_capped_at_baja(conn, score):
    assert bands.band_for(con_evidencia(score, "ninguna")) == "baja"


@pytest.mark.parametrize("evidencia", ["vacantes", "necesidad_declarada", "equipo_relevante"])
@pytest.mark.parametrize(("score", "expected"), [(3, "alta"), (2, "media"), (1, "baja"), (0, None)])
def test_real_evidence_scores_exactly_like_before(conn, evidencia, score, expected):
    """Lo que tiene prueba no cambia: misma banda que una ficha sin el campo."""
    assert bands.band_for(con_evidencia(score, evidencia)) == expected
    assert bands.band_for(dossier(score)) == expected


@pytest.mark.parametrize(("score", "expected"), [(1, "baja"), (0, None)])
def test_no_evidence_never_lifts_a_low_score(conn, score, expected):
    """Es un tope, no un suelo: un 0 sin evidencia sigue sin banda."""
    assert bands.band_for(con_evidencia(score, "ninguna")) == expected


@pytest.mark.parametrize(("score", "expected"), [(3, "alta"), (2, "media"), (1, "baja"), (0, None)])
def test_a_stored_dossier_without_the_field_keeps_its_band(conn, score, expected):
    """Las fichas guardadas antes del campo no lo traen: nada cambia para ellas."""
    assert "evidencia" not in dossier(score)["encaje_handoff"]
    assert bands.band_for(dossier(score)) == expected


@pytest.mark.parametrize(("score", "expected"), [(3, "alta"), (2, "media"), (1, "baja"), (0, None)])
def test_with_the_lock_off_the_band_comes_from_the_score_alone(restore_config, score, expected):
    db.execute("update config set value = 'false'::jsonb where key = 'exigir_evidencia'")
    assert bands.band_for(con_evidencia(score, "ninguna")) == expected


def test_the_lock_is_on_by_default_and_survives_a_missing_or_broken_key(restore_config):
    assert db.fetch_one("select value from config where key = 'exigir_evidencia'")["value"] is True
    db.execute("update config set value = '0'::jsonb where key = 'exigir_evidencia'")
    assert bands.band_for(con_evidencia(3, "ninguna")) == "baja"
    db.execute("delete from config where key = 'exigir_evidencia'")
    assert bands.band_for(con_evidencia(3, "ninguna")) == "baja"


def test_no_evidence_wins_over_an_unconfirmed_company(conn):
    payload = {
        **con_evidencia(3, "ninguna"),
        "empresa_no_confirmada": {"dominio_adivinado": "handoff.ai"},
    }
    assert bands.band_for(payload) == "baja"


def test_a_silenced_score_is_not_resurrected_by_the_cap(restore_config):
    """Con banda_baja_min por encima de todo, tampoco lo sin evidencia entra."""
    db.execute("update config set value = '4'::jsonb where key = 'banda_baja_min'")
    db.execute("update config set value = '4'::jsonb where key = 'banda_media_min'")
    db.execute("update config set value = '4'::jsonb where key = 'banda_alta_min'")
    assert bands.band_for(con_evidencia(3, "ninguna")) is None
