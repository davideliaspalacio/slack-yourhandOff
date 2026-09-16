# Plan 3 — Entrega y decisión humana

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que un dossier recién guardado llegue a Anthony como una tarjeta accionable en el Slack de Handoff, con SMS para lo más caliente y un email diario para el resto, y que sus tres botones actualicen la base.

**Architecture:** El worker gana un paso final (`deliver`) que corre justo después de guardar un dossier: calcula la banda a partir de `encaje_handoff.puntuacion`, publica la tarjeta con el bot de Handoff y anota la entrega. Un segundo servicio en Railway (FastAPI) recibe los clics de los botones, verifica la firma de Slack y cambia el estado de la persona. Un tercer servicio, programado una vez al día, manda el email de resumen.

**Tech Stack:** Python 3.13, slack-sdk (cliente bot aparte del lector), FastAPI + uvicorn, Twilio SDK, Resend por HTTP, Postgres (Supabase Cloud), Railway.

## Global Constraints

- **El agente nunca escribe en el Slack del Founders Club.** El bot de Handoff usa su propio token y solo puede publicar en el canal de `HANDOFF_SLACK_CHANNEL_ID`; el lector del Founders Club sigue siendo de solo lectura.
- **Ninguna llamada a un servicio de pago sin pasar por el ledger.** Cada SMS y cada email escriben una fila en `cost_events`.
- **Las paradas del sistema** (kill switch, tope mensual) cortan la entrega igual que el research, y nunca pierden la tarea.
- **Una persona descartada no vuelve a generar ni tarjeta, ni SMS, ni línea de email.**
- **Nada de reintentos ciegos:** cada entrega se registra en `deliveries`, y una entrega ya hecha no se repite.
- **Los tests nunca tocan la base de desarrollo ni llaman a servicios reales** (Slack, Twilio, Resend, OpenAI).
- **Los topes y umbrales viven en la tabla `config`**, cambiables sin redeploy.
- Estilo del repo: código y comentarios como los existentes, líneas de 100 caracteres, `ruff` limpio.

---

## Estructura de archivos

```
supabase/migrations/0005_entrega.sql        tabla deliveries + filas de config
src/handoff_agent/db_config.py              leer valores de la tabla config
src/handoff_agent/delivery/__init__.py
src/handoff_agent/delivery/bands.py         puntuación del dossier -> banda
src/handoff_agent/delivery/card.py          bloques de la tarjeta de Slack
src/handoff_agent/delivery/slack_writer.py  bot de Handoff: publicar y actualizar
src/handoff_agent/delivery/sms.py           Twilio, tope diario y horario
src/handoff_agent/delivery/digest.py        email diario por Resend
src/handoff_agent/delivery/deliver.py       orquesta: banda -> tarjeta -> SMS
src/handoff_agent/web/app.py                FastAPI: recibe los clics
src/handoff_agent/web/slack_signature.py    verificación de firma
railway.web.json                            servicio del receptor
railway.digest.json                         servicio programado del email
```

---

### Task 1: Tabla de entregas y valores de configuración

**Files:**
- Create: `supabase/migrations/0005_entrega.sql`
- Create: `src/handoff_agent/db_config.py`
- Test: `tests/test_db_config.py`, `tests/test_schema.py` (modificar)

**Interfaces:**
- Produces: tabla `deliveries`; `db_config.value(key: str, default: T) -> T`.

- [ ] **Step 1: Escribir la migración**

```sql
-- supabase/migrations/0005_entrega.sql
-- Una fila por cada aviso enviado. Es lo que impide avisar dos veces de lo
-- mismo y lo que deja ver, por persona, qué se le mandó y cuándo.
create table deliveries (
    id            uuid primary key default gen_random_uuid(),
    prospect_id   uuid not null references prospects (id) on delete cascade,
    kind          text not null check (kind in ('slack', 'sms', 'email')),
    band          text not null check (band in ('alta', 'media', 'baja')),
    dossier_version integer,
    channel_id    text,
    message_ts    text,
    external_id   text,
    detail        jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    -- Postgres trata cada NULL como distinto a efectos de unicidad: sin este
    -- check, el índice único de abajo no evita dos tarjetas de Slack con
    -- dossier_version nulo para la misma persona.
    constraint deliveries_slack_has_version
        check (kind <> 'slack' or dossier_version is not null)
);

create index deliveries_prospect_idx on deliveries (prospect_id, created_at desc);
create index deliveries_kind_idx on deliveries (kind, created_at desc);

-- Una tarjeta por persona y versión de dossier: si el worker reintenta, no
-- se publica dos veces lo mismo.
create unique index deliveries_one_card
    on deliveries (prospect_id, dossier_version)
    where kind = 'slack';

alter table deliveries enable row level security;

insert into config (key, value) values
    ('banda_alta_min',  '3'::jsonb),
    ('banda_media_min', '2'::jsonb),
    ('banda_baja_min',  '1'::jsonb),
    ('sms_por_dia',     '3'::jsonb),
    ('sms_hora_inicio', '7'::jsonb),
    ('sms_hora_fin',    '21'::jsonb),
    ('zona_horaria',    '"America/New_York"'::jsonb)
on conflict (key) do nothing;
```

- [ ] **Step 2: Escribir el test de la configuración**

```python
# tests/test_db_config.py
from handoff_agent import db, db_config


def test_value_reads_the_row(conn):
    assert db_config.value("sms_por_dia", 99) == 3


def test_value_falls_back_when_the_row_is_missing(conn):
    assert db_config.value("no_existe", 7) == 7


def test_value_falls_back_when_the_type_is_wrong(conn, caplog):
    """Un valor de tipo distinto al del defecto no vale, sea cual sea ese tipo.

    config es tabla de sesión (no está en TABLES_TO_CLEAN): hay que devolver
    sms_por_dia a como lo deja la migración o el siguiente test la hereda rota.
    """
    db.execute("update config set value = '\"tres\"'::jsonb where key = 'sms_por_dia'")
    try:
        assert db_config.value("sms_por_dia", 3) == 3
        assert "sms_por_dia" in caplog.text
    finally:
        db.execute("update config set value = '3'::jsonb where key = 'sms_por_dia'")


def test_value_reads_a_real_stored_true(conn):
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    try:
        assert db_config.value("kill_switch", False) is True
    finally:
        db.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")


def test_value_reads_a_real_stored_false(conn):
    # kill_switch ya sale en false de la migración; lo dejamos explícito para
    # que el test no dependa de ese orden.
    db.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")
    assert db_config.value("kill_switch", True) is False


def test_value_rejects_a_stored_true_when_the_default_is_an_int(conn, caplog):
    """bool es subclase de int en Python: sin este caso aparte, un true
    guardado colaría como 1 para un default entero."""
    db.execute("update config set value = 'true'::jsonb where key = 'sms_por_dia'")
    try:
        assert db_config.value("sms_por_dia", 3) == 3
        assert "sms_por_dia" in caplog.text
    finally:
        db.execute("update config set value = '3'::jsonb where key = 'sms_por_dia'")
```

- [ ] **Step 3: Ejecutar y ver fallar**

Run: `uv run pytest tests/test_db_config.py -v`
Expected: FAIL, `ModuleNotFoundError: handoff_agent.db_config`

- [ ] **Step 4: Implementar**

```python
# src/handoff_agent/db_config.py
"""Valores operativos que viven en la tabla config, no en el entorno.

Los topes y umbrales tienen que poder cambiarse sin redeploy: es lo que pide
el spec y lo que permite parar o aflojar el sistema en caliente.
"""

from __future__ import annotations

import logging

from . import db

logger = logging.getLogger(__name__)


def value[T](key: str, default: T) -> T:
    row = db.fetch_one("select value from config where key = %s", (key,))
    if row is None:
        return default
    found = row["value"]
    # bool es subclase de int en Python: sin este caso aparte, un default
    # entero aceptaría un true/false guardado como si fuera 1/0, y un default
    # booleano nunca podría leer un valor real de la tabla (isinstance(found,
    # bool) siempre lo habría rechazado).
    if isinstance(default, bool):
        valid = isinstance(found, bool)
    else:
        valid = isinstance(found, type(default)) and not isinstance(found, bool)
    if not valid:
        logger.warning("config %s: valor inválido %r, se usa %r", key, found, default)
        return default
    return found
```

Nota: `def value[T](...)` usa la sintaxis de genéricos de PEP 695 (Python 3.13),
que no necesita importar `TypeVar`.

- [ ] **Step 5: Añadir la tabla al test de esquema**

En `tests/test_schema.py`, añadir `"deliveries"` a la lista de tablas esperadas y a la comprobación de RLS. Añadir también dos pruebas para el dedup: una tarjeta de Slack repetida con la misma `dossier_version` debe chocar con `UniqueViolation`, y una tarjeta de Slack sin `dossier_version` debe chocar con `CheckViolation` (si no, el índice único no lo cubre: Postgres trata cada NULL como distinto). Añadir también `"deliveries"` a `TABLES_TO_CLEAN` en `tests/conftest.py` para que un test que inserte filas no contamine el siguiente.

- [ ] **Step 6: Aplicar y verificar**

Run: `supabase migration up --linked` y `uv run pytest tests/test_db_config.py tests/test_schema.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add supabase/migrations/0005_entrega.sql src/handoff_agent/db_config.py tests/test_db_config.py tests/test_schema.py
git commit -m "feat: tabla de entregas y lectura de config operativa"
```

---

### Task 2: De la puntuación del dossier a la banda

**Files:**
- Create: `src/handoff_agent/delivery/__init__.py`, `src/handoff_agent/delivery/bands.py`
- Test: `tests/test_bands.py`

**Interfaces:**
- Consumes: `db_config.value`.
- Produces: `bands.band_for(dossier: dict) -> str | None` devolviendo `"alta" | "media" | "baja" | None`.

- [ ] **Step 1: Escribir el test**

```python
# tests/test_bands.py
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

    Esto prueba que la escalera NO tiene el caso especial removido en e227cea.
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
    """Para silenciar una puntuación entera, sube banda_baja_min por encima."""
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
    """bool es subclase de int en Python, pero rechazamos bool explícitamente."""
    assert bands.band_for(dossier(True)) is None


def test_a_dossier_without_a_usable_score_is_not_delivered(conn):
    assert bands.band_for({}) is None
    assert bands.band_for({"encaje_handoff": {"puntuacion": "alta"}}) is None
```

- [ ] **Step 2: Ejecutar y ver fallar**

Run: `uv run pytest tests/test_bands.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implementar**

```python
# src/handoff_agent/delivery/bands.py
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
```

- [ ] **Step 4: Ejecutar y ver pasar**

Run: `uv run pytest tests/test_bands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/delivery tests/test_bands.py
git commit -m "feat: la banda de entrega sale de la puntuacion del dossier"
```

---

### Task 3: El enlace al mensaje original

**Files:**
- Modify: `src/handoff_agent/slack_client.py`
- Test: `tests/test_slack_client.py`, `tests/slack_fakes.py`

**Interfaces:**
- Produces: `FoundersClubReader.permalink(channel: str, ts: str) -> str | None`.

- [ ] **Step 1: Escribir el test**

```python
# tests/test_slack_client.py  (añadir)
def test_permalink_asks_slack_for_the_link(monkeypatch):
    calls = []

    class FakeWeb:
        def chat_getPermalink(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "permalink": "https://handoff.slack.com/archives/C1/p1726000000000100"}

    reader = sc.FoundersClubReader(token="xoxp-test", client=FakeWeb())
    assert reader.permalink("C1", "1726000000.000100").endswith("p1726000000000100")
    assert calls[0] == {"channel": "C1", "message_ts": "1726000000.000100"}


def test_permalink_returns_none_when_slack_says_no(monkeypatch):
    """Una tarjeta sin enlace sigue siendo útil; una excepción aquí tumbaría
    la entrega entera."""

    class FakeWeb:
        def chat_getPermalink(self, **kwargs):
            raise sc.SlackApiError("message_not_found", response={"error": "message_not_found"})

    reader = sc.FoundersClubReader(token="xoxp-test", client=FakeWeb())
    assert reader.permalink("C1", "1.0") is None
```

- [ ] **Step 2: Ejecutar y ver fallar**

Run: `uv run pytest tests/test_slack_client.py -k permalink -v`
Expected: FAIL, `AttributeError: 'FoundersClubReader' object has no attribute 'permalink'`

- [ ] **Step 3: Implementar**

```python
# src/handoff_agent/slack_client.py  (añadir al final de la clase)
    def permalink(self, channel: str, ts: str) -> str | None:
        """Enlace al mensaje real. Es de lectura, como el resto de la clase.

        Todo el sistema existe para producir el clic en este enlace, pero si
        Slack no lo da (mensaje borrado, canal archivado) la tarjeta se manda
        igual: vale más una tarjeta sin enlace que ninguna tarjeta.
        """
        try:
            return self._call("chat_getPermalink", channel=channel, message_ts=ts)["permalink"]
        except SlackUnavailable:
            logger.warning("sin permalink para %s/%s", channel, ts)
            return None
```

Añadir también `permalink` al test que fija la superficie de solo lectura (`test_the_reader_only_exposes_reads`) y a `FakeReader` en `tests/slack_fakes.py`:

```python
# tests/slack_fakes.py
    def permalink(self, channel: str, ts: str) -> str | None:
        return f"https://fake.slack.com/archives/{channel}/p{ts.replace('.', '')}"
```

- [ ] **Step 4: Ejecutar y ver pasar**

Run: `uv run pytest tests/test_slack_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/slack_client.py tests/test_slack_client.py tests/slack_fakes.py
git commit -m "feat: permalink del mensaje original, degradando a None"
```

---

### Task 4: La tarjeta

**Files:**
- Create: `src/handoff_agent/delivery/card.py`
- Test: `tests/test_card.py`

**Interfaces:**
- Produces: `card.build(person: dict, dossier: dict, band: str, message: dict | None, permalink: str | None) -> list[dict]`.

**Forma de la tarjeta** (spec §10): banda y antigüedad, quién es y su empresa, la cita textual, por qué importa (las señales con su fuente), el ángulo sugerido, y los cuatro botones.

- [ ] **Step 1: Escribir el test**

```python
# tests/test_card.py
import json

from handoff_agent.delivery import card

PERSON = {"id": "11111111-1111-1111-1111-111111111111", "full_name": "Ada Ruiz",
          "company_name": "Acme", "slack_user_id": "U1"}
DOSSIER = {
    "persona": {"nombre": "Ada Ruiz", "cargo": "CEO"},
    "empresa": {"nombre": "Acme", "empleados_aprox": 60},
    "contratacion": {"vacantes_abiertas": 6, "roles_deslocalizables": ["support", "ops"]},
    "senales_contexto": [{"hecho": "Levantó Series A hace 3 meses", "fuente": "https://tc.com/a"}],
    "encaje_handoff": {"puntuacion": 3, "razon": "Contrata soporte ahora mismo"},
    "resumen": "CEO de Acme, 60 personas, contratando soporte.",
}
MESSAGE = {"text": "Our support queue is eating my whole week", "ts": "1726000000.000100"}


def blocks_text(blocks):
    return json.dumps(blocks, ensure_ascii=False)


def test_the_card_leads_with_the_person_and_the_quote():
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, "https://slack.com/p1")
    text = blocks_text(blocks)
    assert "Ada Ruiz" in text and "CEO" in text and "Acme" in text
    assert "support queue is eating my whole week" in text


def test_the_card_explains_why_it_matters_with_its_sources():
    text = blocks_text(card.build(PERSON, DOSSIER, "alta", MESSAGE, None))
    assert "Series A" in text and "https://tc.com/a" in text
    assert "Contrata soporte ahora mismo" in text


def test_the_buttons_carry_the_person_and_the_action():
    blocks = card.build(PERSON, DOSSIER, "alta", MESSAGE, "https://slack.com/p1")
    actions = [b for b in blocks if b["type"] == "actions"][0]["elements"]
    assert [e["action_id"] for e in actions[:3]] == ["contactado", "descartar", "investigar_mas"]
    assert all(e["value"] == PERSON["id"] for e in actions[:3])
    link = actions[3]
    assert link["url"] == "https://slack.com/p1"


def test_without_a_permalink_there_is_no_broken_button():
    blocks = card.build(PERSON, DOSSIER, "media", MESSAGE, None)
    actions = [b for b in blocks if b["type"] == "actions"][0]["elements"]
    assert all("url" not in e for e in actions)


def test_a_new_member_has_no_quote_to_show():
    """Un miembro nuevo que aún no ha escrito no tiene mensaje que citar."""
    text = blocks_text(card.build(PERSON, DOSSIER, "baja", None, None))
    assert "Ada Ruiz" in text
    assert "Dijo en" not in text


def test_the_quote_cannot_break_the_card():
    """El texto lo escribió un tercero: no puede romper el formato ni inyectar
    bloques."""
    hostile = {"text": "```\n*/ }] injected", "ts": "1.0"}
    blocks = card.build(PERSON, DOSSIER, "alta", hostile, None)
    assert isinstance(blocks, list)
    assert "injected" in blocks_text(blocks)
```

- [ ] **Step 2: Ejecutar y ver fallar**

Run: `uv run pytest tests/test_card.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implementar**

```python
# src/handoff_agent/delivery/card.py
"""La tarjeta de Slack: el producto que ve Anthony.

Todo el sistema existe para producir un clic en "Ver mensaje original", así que
la tarjeta cita textualmente, explica por qué importa con fuentes, y propone un
ángulo. El texto ajeno se cita como bloque de cita y se recorta: no puede
romper el formato ni alargar la tarjeta sin fin.
"""

from __future__ import annotations

QUOTE_CHARS = 300
MAX_SIGNALS = 3
EMOJI = {"alta": "🔥", "media": "👀", "baja": "📋"}


def _quote(text: str) -> str:
    clean = " ".join((text or "").split())[:QUOTE_CHARS]
    return "\n".join(f"> {line}" for line in clean.splitlines() or [""])


def _headline(person: dict, dossier: dict, band: str) -> str:
    persona = dossier.get("persona") or {}
    empresa = dossier.get("empresa") or {}
    name = persona.get("nombre") or person.get("full_name") or person["slack_user_id"]
    role = persona.get("cargo")
    company = empresa.get("nombre") or person.get("company_name")
    size = empresa.get("empleados_aprox")
    bits = [b for b in (role, company) if b]
    tail = f" (~{size} personas)" if size else ""
    return f"*{name}*" + (f" — {', '.join(bits)}{tail}" if bits else "")


def build(
    person: dict,
    dossier: dict,
    band: str,
    message: dict | None,
    permalink: str | None,
) -> list[dict]:
    fit = dossier.get("encaje_handoff") or {}
    hiring = dossier.get("contratacion") or {}
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{EMOJI.get(band, '')} *SEÑAL {band.upper()}* · encaje {fit.get('puntuacion')}/3\n"
                + _headline(person, dossier, band),
            },
        }
    ]

    if message and (message.get("text") or "").strip():
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "Dijo:\n" + _quote(message["text"])}}
        )

    reasons = []
    if fit.get("razon"):
        reasons.append(f"· {fit['razon']}")
    for signal in (dossier.get("senales_contexto") or [])[:MAX_SIGNALS]:
        reasons.append(f"· <{signal.get('fuente')}|{signal.get('hecho')}>")
    if hiring.get("vacantes_abiertas"):
        roles = ", ".join(hiring.get("roles_deslocalizables") or [])
        reasons.append(f"· {hiring['vacantes_abiertas']} vacantes abiertas" + (f": {roles}" if roles else ""))
    if reasons:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Por qué importa*\n" + "\n".join(reasons)}}
        )

    if dossier.get("resumen"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": dossier["resumen"]}]})

    elements = [
        {"type": "button", "action_id": "contactado", "text": {"type": "plain_text", "text": "Contactado"},
         "value": person["id"], "style": "primary"},
        {"type": "button", "action_id": "descartar", "text": {"type": "plain_text", "text": "Descartar"},
         "value": person["id"], "style": "danger"},
        {"type": "button", "action_id": "investigar_mas", "text": {"type": "plain_text", "text": "Investigar más"},
         "value": person["id"]},
    ]
    if permalink:
        elements.append(
            {"type": "button", "action_id": "ver_original",
             "text": {"type": "plain_text", "text": "Ver mensaje original"}, "url": permalink}
        )
    blocks.append({"type": "actions", "elements": elements})
    return blocks
```

- [ ] **Step 4: Ejecutar y ver pasar**

Run: `uv run pytest tests/test_card.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/delivery/card.py tests/test_card.py
git commit -m "feat: tarjeta de Slack con cita, razones y botones"
```

---

### Task 5: El bot que publica en el Slack de Handoff

**Files:**
- Create: `src/handoff_agent/delivery/slack_writer.py`
- Modify: `src/handoff_agent/config.py`, `.env.example`
- Test: `tests/test_slack_writer.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `Settings.handoff_bot_token`, `Settings.handoff_channel_id`, `Settings.founders_club_channel_ids`.
- Produces: `slack_writer.post_card(blocks, text) -> tuple[str, str]` (canal y ts), `slack_writer.update_card(channel, ts, blocks, text) -> None`, `slack_writer.SlackWriteRefused`.

- [ ] **Step 1: Escribir el test**

```python
# tests/test_slack_writer.py
import pytest

from handoff_agent.delivery import slack_writer


class FakeBot:
    def __init__(self):
        self.posted, self.updated = [], []

    def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True, "channel": kwargs["channel"], "ts": "1726000001.000200"}

    def chat_update(self, **kwargs):
        self.updated.append(kwargs)
        return {"ok": True}


@pytest.fixture
def bot(monkeypatch):
    fake = FakeBot()
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    monkeypatch.setattr(slack_writer, "_client", lambda: fake)
    return fake


def test_posting_goes_to_the_handoff_channel(bot):
    channel, ts = slack_writer.post_card([{"type": "divider"}], "señal alta")
    assert (channel, ts) == ("CHANDOFF", "1726000001.000200")
    assert bot.posted[0]["channel"] == "CHANDOFF"
    assert bot.posted[0]["text"] == "señal alta"


def test_it_refuses_to_write_in_a_founders_club_channel(bot, monkeypatch):
    """La invariante del proyecto: el agente nunca escribe allí. Un canal mal
    configurado no puede convertirse en un mensaje publicado."""
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "CHANDOFF,C999")
    with pytest.raises(slack_writer.SlackWriteRefused):
        slack_writer.post_card([{"type": "divider"}], "señal")
    assert bot.posted == []


def test_without_a_bot_token_it_refuses_instead_of_guessing(bot, monkeypatch):
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN")
    with pytest.raises(slack_writer.SlackWriteRefused):
        slack_writer.post_card([{"type": "divider"}], "señal")


def test_updating_replaces_the_card_in_place(bot):
    slack_writer.update_card("CHANDOFF", "1.0", [{"type": "divider"}], "actualizada")
    assert bot.updated[0]["ts"] == "1.0"
```

- [ ] **Step 2: Ejecutar y ver fallar**

Run: `uv run pytest tests/test_slack_writer.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implementar**

```python
# src/handoff_agent/delivery/slack_writer.py
"""El único sitio del código que escribe en Slack, y solo en el de Handoff.

El lector del Founders Club y este escritor son clases distintas con tokens
distintos a propósito: así "el agente nunca escribe en el Founders Club" es
algo que impone el código, no una intención. Antes de publicar se comprueba
que el canal de destino no sea uno de los vigilados.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from slack_sdk import WebClient

from ..config import load_settings

logger = logging.getLogger(__name__)


class SlackWriteRefused(RuntimeError):
    """Falta el token del bot, falta el canal, o el destino es un canal vigilado."""


@lru_cache(maxsize=1)
def _client() -> WebClient:
    settings = load_settings()
    if not settings.handoff_bot_token:
        raise SlackWriteRefused("falta HANDOFF_SLACK_BOT_TOKEN")
    return WebClient(token=settings.handoff_bot_token, timeout=settings.http_timeout_seconds)


def _target_channel() -> str:
    settings = load_settings()
    channel = settings.handoff_channel_id
    if not channel:
        raise SlackWriteRefused("falta HANDOFF_SLACK_CHANNEL_ID")
    if channel in settings.slack_channel_ids:
        raise SlackWriteRefused(f"{channel} es un canal vigilado del Founders Club: no se escribe allí")
    return channel


def post_card(blocks: list[dict], text: str) -> tuple[str, str]:
    channel = _target_channel()
    response = _client().chat_postMessage(channel=channel, blocks=blocks, text=text)
    return response["channel"], response["ts"]


def update_card(channel: str, ts: str, blocks: list[dict], text: str) -> None:
    _client().chat_update(channel=channel, ts=ts, blocks=blocks, text=text)
```

En `config.py`, añadir a `Settings` y a `load_settings`:

```python
    handoff_bot_token: str | None
    handoff_channel_id: str | None
    slack_signing_secret: str | None
```
```python
        handoff_bot_token=os.environ.get("HANDOFF_SLACK_BOT_TOKEN") or None,
        handoff_channel_id=os.environ.get("HANDOFF_SLACK_CHANNEL_ID") or None,
        slack_signing_secret=os.environ.get("SLACK_SIGNING_SECRET") or None,
```

Añadir las tres a `ENV_THAT_MUST_NOT_LEAK` en `tests/conftest.py` y a `.env.example`.

- [ ] **Step 4: Ejecutar y ver pasar**

Run: `uv run pytest tests/test_slack_writer.py tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/delivery/slack_writer.py src/handoff_agent/config.py tests/test_slack_writer.py tests/test_config.py tests/conftest.py .env.example
git commit -m "feat: bot de Handoff que publica tarjetas y se niega a tocar el Founders Club"
```

---

### Task 6: Entregar la tarjeta tras el research

**Files:**
- Create: `src/handoff_agent/delivery/deliver.py`
- Modify: `src/handoff_agent/ingest/research_runner.py`
- Test: `tests/test_deliver.py`

**Interfaces:**
- Consumes: `bands.band_for`, `card.build`, `slack_writer.post_card`, `FoundersClubReader.permalink`.
- Produces: `deliver.deliver_for(prospect_id: str, reader) -> str | None` (la banda entregada, o None).

- [ ] **Step 1: Escribir el test**

```python
# tests/test_deliver.py
from handoff_agent import db
from handoff_agent.delivery import deliver
from handoff_agent.tools import prospects
from tests.slack_fakes import FakeReader
from tests.test_card import DOSSIER


def a_person(state="investigado", score=3):
    person = prospects.upsert_prospect("U1", full_name="Ada Ruiz")
    db.execute("update prospects set state = %s where id = %s", (state, person["id"]))
    dossier = {**DOSSIER, "encaje_handoff": {"puntuacion": score, "razon": "r"}}
    prospects.save_dossier(person["id"], dossier, [{"url": "https://acme.com", "kind": "home", "title": "Acme"}])
    return person


def test_a_high_band_person_gets_a_card(conn, monkeypatch):
    posted = []
    monkeypatch.setattr(deliver.slack_writer, "post_card",
                        lambda blocks, text: posted.append((blocks, text)) or ("CHANDOFF", "1.1"))
    person = a_person()
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    row = db.fetch_one("select kind, band, channel_id, message_ts from deliveries")
    assert (row["kind"], row["band"], row["channel_id"], row["message_ts"]) == ("slack", "alta", "CHANDOFF", "1.1")
    assert posted


def test_the_same_dossier_is_never_delivered_twice(conn, monkeypatch):
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: ("CHANDOFF", "1.1"))
    person = a_person()
    assert deliver.deliver_for(person["id"], FakeReader()) == "alta"
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 1


def test_a_discarded_person_never_generates_an_alert(conn, monkeypatch):
    """Descartar no es solo dejar de gastar: es dejar de avisar."""
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: ("CHANDOFF", "1.1"))
    person = a_person(state="descartado")
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0


def test_score_zero_is_not_delivered(conn, monkeypatch):
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: ("CHANDOFF", "1.1"))
    person = a_person(score=0)
    assert deliver.deliver_for(person["id"], FakeReader()) is None


def test_a_low_band_person_waits_for_the_digest(conn, monkeypatch):
    """Banda baja no interrumpe: se anota para el email diario, sin tarjeta."""
    called = []
    monkeypatch.setattr(deliver.slack_writer, "post_card", lambda blocks, text: called.append(1))
    person = a_person(score=1)
    assert deliver.deliver_for(person["id"], FakeReader()) == "baja"
    assert called == []
    assert db.fetch_one("select count(*) as n from deliveries")["n"] == 0


def test_slack_failing_does_not_lose_the_research(conn, monkeypatch):
    def boom(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(deliver.slack_writer, "post_card", boom)
    person = a_person()
    assert deliver.deliver_for(person["id"], FakeReader()) is None
    assert db.fetch_one("select count(*) as n from agent_actions where action = 'entrega_fallida'")["n"] == 1
```

- [ ] **Step 2: Ejecutar y ver fallar**

Run: `uv run pytest tests/test_deliver.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implementar**

```python
# src/handoff_agent/delivery/deliver.py
"""Del dossier guardado a la tarjeta en el Slack de Handoff.

Corre justo después del research, con el dossier fresco. Nunca tumba el
research: si Slack falla, la investigación ya está pagada y guardada, y la
entrega se anota como fallida para reintentarla a mano o desde el panel.
"""

from __future__ import annotations

import logging

from .. import db, ledger
from ..tools import prospects
from . import bands, card, slack_writer

logger = logging.getLogger(__name__)
NO_ALERT_STATES = ("descartado",)


def _last_message(slack_user_id: str) -> dict | None:
    return db.fetch_one(
        "select channel_id, ts, text from slack_messages "
        "where user_id = %s and status = 'nuevo' order by ts::numeric desc limit 1",
        (slack_user_id,),
    )


def deliver_for(prospect_id: str, reader) -> str | None:
    person = db.fetch_one("select * from prospects where id = %s", (prospect_id,))
    if person is None or person["state"] in NO_ALERT_STATES:
        return None

    dossier = db.fetch_one(
        "select version, content from dossiers where prospect_id = %s order by version desc limit 1",
        (prospect_id,),
    )
    if dossier is None:
        return None

    band = bands.band_for(dossier["content"])
    if band is None:
        return None
    if band == "baja":
        # Banda baja no interrumpe: la recoge el email diario leyendo dossiers.
        return band

    already = db.fetch_one(
        "select id from deliveries where prospect_id = %s and dossier_version = %s and kind = 'slack'",
        (prospect_id, dossier["version"]),
    )
    if already:
        return None

    message = _last_message(person["slack_user_id"])
    permalink = reader.permalink(message["channel_id"], message["ts"]) if message else None
    blocks = card.build(person, dossier["content"], band, message, permalink)
    summary = f"Señal {band}: {person['full_name'] or person['slack_user_id']}"

    try:
        channel, ts = slack_writer.post_card(blocks, summary)
    except Exception as exc:  # noqa: BLE001 - el research ya está pagado y guardado
        logger.exception("no se pudo publicar la tarjeta")
        ledger.record_action("entrega_fallida", {"motivo": f"{type(exc).__name__}: {exc}"},
                             prospect_id=prospect_id)
        return None

    db.execute(
        "insert into deliveries (prospect_id, kind, band, dossier_version, channel_id, message_ts) "
        "values (%s, 'slack', %s, %s, %s, %s)",
        (prospect_id, band, dossier["version"], channel, ts),
    )
    return band
```

En `research_runner.py`, tras un `complete()` con estado `hecho`, llamar a la entrega:

```python
    # El SMS se engancha aquí en la Task 7; esta tarea solo publica la tarjeta.
    if outcome.status in ("investigado", "incompleto") and outcome.version:
        deliver_for(outcome.prospect_id, reader)
```

- [ ] **Step 4: Ejecutar y ver pasar**

Run: `uv run pytest tests/test_deliver.py tests/test_research_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/delivery/deliver.py src/handoff_agent/ingest/research_runner.py tests/test_deliver.py
git commit -m "feat: publicar la tarjeta al terminar el research, sin arriesgar el dossier"
```

---

### Task 7: SMS para banda alta, con tope y horario

**Files:**
- Create: `src/handoff_agent/delivery/sms.py`
- Modify: `pyproject.toml` (dependencia `twilio`), `src/handoff_agent/config.py`, `.env.example`
- Test: `tests/test_sms.py`

**Interfaces:**
- Produces: `sms.maybe_send(prospect_id: str, band: str, now: datetime | None = None) -> bool`.

**Reglas (spec §10):** solo banda alta, máximo 3 al día, nada entre las 21:00 y las 07:00 en la zona horaria de Anthony, una línea y el enlace a Slack, nunca el dossier.

- [ ] **Step 1: Escribir el test**

```python
# tests/test_sms.py
from datetime import datetime
from zoneinfo import ZoneInfo

from handoff_agent import db
from handoff_agent.delivery import sms
from tests.test_deliver import a_person

NY = ZoneInfo("America/New_York")
MIDDAY = datetime(2026, 9, 16, 12, 0, tzinfo=NY)
NIGHT = datetime(2026, 9, 16, 23, 30, tzinfo=NY)


def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(sms, "_send", lambda to, body: calls.append((to, body)) or "SM123")
    return calls


def test_a_high_band_person_gets_one_sms(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person()
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is True
    assert len(calls) == 1
    assert "Ada Ruiz" in calls[0][1]
    row = db.fetch_one("select kind, external_id from deliveries where kind = 'sms'")
    assert row["external_id"] == "SM123"


def test_medium_and_low_never_send_sms(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person(score=2)
    assert sms.maybe_send(person["id"], "media", now=MIDDAY) is False
    assert calls == []


def test_nothing_is_sent_at_night(conn, monkeypatch):
    calls = sent(monkeypatch)
    person = a_person()
    assert sms.maybe_send(person["id"], "alta", now=NIGHT) is False
    assert calls == []


def test_the_daily_cap_holds(conn, monkeypatch):
    calls = sent(monkeypatch)
    for i in range(4):
        person = a_person()
        db.execute("update prospects set slack_user_id = %s where id = %s", (f"U{i}", person["id"]))
        sms.maybe_send(person["id"], "alta", now=MIDDAY)
    assert len(calls) == 3


def test_every_sms_lands_in_the_cost_ledger(conn, monkeypatch):
    sent(monkeypatch)
    person = a_person()
    sms.maybe_send(person["id"], "alta", now=MIDDAY)
    assert db.fetch_one("select source from cost_events")["source"] == "twilio_sms"


def test_twilio_failing_never_breaks_the_pipeline(conn, monkeypatch):
    def boom(to, body):
        raise RuntimeError("twilio caído")

    monkeypatch.setattr(sms, "_send", boom)
    person = a_person()
    assert sms.maybe_send(person["id"], "alta", now=MIDDAY) is False
```

- [ ] **Step 2: Ejecutar y ver fallar**

Run: `uv run pytest tests/test_sms.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implementar**

```python
# src/handoff_agent/delivery/sms.py
"""SMS solo para lo más caliente, con tope duro y horario.

Tres alertas malas seguidas y Anthony deja de abrirlas. El SMS lleva una línea
y el enlace a Slack, nunca el dossier: el contexto está en la tarjeta.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .. import db, db_config, ledger
from ..config import load_settings

logger = logging.getLogger(__name__)
PRICE_PER_SMS = Decimal("0.0079")


def _send(to: str, body: str) -> str:
    from twilio.rest import Client

    settings = load_settings()
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return client.messages.create(to=to, from_=settings.twilio_from, body=body).sid


def _within_hours(now: datetime) -> bool:
    zone = ZoneInfo(db_config.value("zona_horaria", "America/New_York"))
    hour = now.astimezone(zone).hour
    return db_config.value("sms_hora_inicio", 7) <= hour < db_config.value("sms_hora_fin", 21)


def _sent_today(now: datetime) -> int:
    return db.fetch_one(
        "select count(*) as n from deliveries where kind = 'sms' and created_at >= %s",
        (now.astimezone(ZoneInfo(db_config.value("zona_horaria", "America/New_York")))
         .replace(hour=0, minute=0, second=0, microsecond=0),),
    )["n"]


def maybe_send(prospect_id: str, band: str, now: datetime | None = None) -> bool:
    settings = load_settings()
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    if band != "alta" or not settings.twilio_to:
        return False
    if not _within_hours(now):
        logger.info("sms fuera de horario, se omite")
        return False
    if _sent_today(now) >= db_config.value("sms_por_dia", 3):
        logger.info("sms: tope diario alcanzado")
        return False

    person = db.fetch_one("select * from prospects where id = %s", (prospect_id,))
    card = db.fetch_one(
        "select channel_id, message_ts from deliveries "
        "where prospect_id = %s and kind = 'slack' order by created_at desc limit 1",
        (prospect_id,),
    )
    link = ""
    if card:
        link = f" https://app.slack.com/client/{card['channel_id']}/{card['message_ts']}"
    body = f"Señal alta: {person['full_name'] or person['slack_user_id']}" + (
        f" ({person['company_name']})" if person["company_name"] else ""
    ) + link

    try:
        sid = _send(settings.twilio_to, body)
    except Exception as exc:  # noqa: BLE001 - un SMS caído no tumba el pipeline
        logger.exception("no se pudo enviar el SMS")
        ledger.record_action("sms_fallido", {"motivo": f"{type(exc).__name__}: {exc}"},
                             prospect_id=prospect_id)
        return False

    db.execute(
        "insert into deliveries (prospect_id, kind, band, external_id) values (%s, 'sms', %s, %s)",
        (prospect_id, band, sid),
    )
    ledger.record_cost_event("twilio_sms", PRICE_PER_SMS, "SMS de señal alta", prospect_id)
    return True
```

Añadir a `Settings`: `twilio_account_sid`, `twilio_auth_token`, `twilio_from`, `twilio_to`; y `twilio>=9.0` a las dependencias.

- [ ] **Step 4: Ejecutar y ver pasar**

Run: `uv run pytest tests/test_sms.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/delivery/sms.py src/handoff_agent/config.py pyproject.toml uv.lock tests/test_sms.py .env.example
git commit -m "feat: SMS de banda alta con tope diario y horario"
```

---

### Task 8: Email diario de resumen

**Files:**
- Create: `src/handoff_agent/delivery/digest.py`
- Modify: `src/handoff_agent/cli.py` (comando `digest`), `src/handoff_agent/config.py`, `.env.example`
- Create: `railway.digest.json`
- Test: `tests/test_digest.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `digest.build(day: date) -> tuple[str, str]` (asunto, cuerpo HTML); `digest.send_daily(day: date | None = None) -> bool`; comando `handoff digest`.

**Contenido (spec §10):** miembros nuevos investigados, señales de banda baja y resumen de coste del día.

- [ ] **Step 1: Escribir el test**

```python
# tests/test_digest.py
from datetime import date

from handoff_agent import db
from handoff_agent.delivery import digest
from tests.test_deliver import a_person

TODAY = date(2026, 9, 16)


def test_the_digest_lists_low_band_people(conn):
    person = a_person(score=1)
    subject, body = digest.build(TODAY)
    assert "Ada Ruiz" in body
    assert "señal baja" in body.lower()


def test_the_digest_lists_new_members_researched(conn):
    person = a_person(score=1)
    db.execute("insert into research_jobs (slack_user_id, reason, status) values ('U1', 'miembro_nuevo', 'hecho')")
    subject, body = digest.build(TODAY)
    assert "miembro nuevo" in body.lower()


def test_the_digest_shows_the_spend_of_the_day(conn):
    a_person()
    db.execute("insert into llm_calls (stage, model, input_tokens, output_tokens, cost_usd) "
               "values ('research_sintesis', 'gpt-4.1', 100, 50, 0.0217)")
    subject, body = digest.build(TODAY)
    assert "0.02" in body


def test_high_band_people_are_not_repeated_in_the_digest(conn):
    """Ya recibió tarjeta y SMS: repetirlo en el email es ruido."""
    a_person(score=3)
    subject, body = digest.build(TODAY)
    assert "Ada Ruiz" not in body


def test_an_empty_day_sends_nothing(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(digest, "_send_email", lambda subject, body: calls.append(subject))
    assert digest.send_daily(TODAY) is False
    assert calls == []
```

- [ ] **Step 2: Ejecutar y ver fallar**

Run: `uv run pytest tests/test_digest.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implementar**

```python
# src/handoff_agent/delivery/digest.py
"""El email diario: lo que no merece interrumpir, pero sí saberse.

Miembros nuevos investigados, señales de banda baja y el gasto del día. Las
bandas alta y media ya fueron a Slack: repetirlas aquí es ruido.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal

import httpx

from .. import db, ledger
from ..config import load_settings
from . import bands

logger = logging.getLogger(__name__)
RESEND_URL = "https://api.resend.com/emails"
PRICE_PER_EMAIL = Decimal("0")


def _window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, datetime.combine(day, time.max, tzinfo=timezone.utc)


def _researched_today(day: date) -> list[dict]:
    start, end = _window(day)
    return db.fetch_all(
        """
        select p.full_name, p.company_name, p.slack_user_id, d.content, j.reason
        from dossiers d
        join prospects p on p.id = d.prospect_id
        left join research_jobs j on j.slack_user_id = p.slack_user_id and j.status = 'hecho'
        where d.created_at between %s and %s and p.state <> 'descartado'
        order by d.created_at
        """,
        (start, end),
    )


def build(day: date) -> tuple[str, str]:
    rows = _researched_today(day)
    low = [r for r in rows if bands.band_for(r["content"]) == "baja"]
    new_members = [r for r in rows if r["reason"] == "miembro_nuevo"]
    start, end = _window(day)
    spend = db.fetch_one(
        "select coalesce((select sum(cost_usd) from llm_calls where created_at between %s and %s), 0) "
        "+ coalesce((select sum(cost_usd) from cost_events where created_at between %s and %s), 0) as total",
        (start, end, start, end),
    )["total"]

    def line(row):
        who = row["full_name"] or row["slack_user_id"]
        company = f" — {row['company_name']}" if row["company_name"] else ""
        return f"<li>{who}{company}: {row['content'].get('resumen', '')}</li>"

    parts = [f"<h2>Founders Club — {day.isoformat()}</h2>"]
    parts.append(f"<p>Señales de banda baja: {len(low)}</p>")
    if low:
        parts.append("<ul>" + "".join(line(r) for r in low) + "</ul>")
    parts.append(f"<p>Miembros nuevos investigados: {len(new_members)}</p>")
    if new_members:
        parts.append("<ul>" + "".join(line(r) for r in new_members) + "</ul>")
    parts.append(f"<p>Gasto del día: ${Decimal(spend):.4f}</p>")
    return f"Founders Club — {len(low)} señales bajas, ${Decimal(spend):.2f}", "".join(parts)


def _send_email(subject: str, body: str) -> None:
    settings = load_settings()
    response = httpx.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json={"from": settings.digest_from, "to": list(settings.digest_to),
              "subject": subject, "html": body},
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()


def send_daily(day: date | None = None) -> bool:
    settings = load_settings()
    day = day or datetime.now(tz=timezone.utc).date()
    if not settings.resend_api_key or not settings.digest_to:
        logger.warning("digest: sin RESEND_API_KEY o DIGEST_TO, no se envía")
        return False

    rows = _researched_today(day)
    if not rows:
        logger.info("digest: nada que contar hoy")
        return False

    subject, body = build(day)
    try:
        _send_email(subject, body)
    except Exception as exc:  # noqa: BLE001 - el email nunca tumba nada
        logger.exception("no se pudo enviar el resumen diario")
        ledger.record_action("digest_fallido", {"motivo": f"{type(exc).__name__}: {exc}"})
        return False
    ledger.record_action("digest_enviado", {"dia": day.isoformat(), "filas": len(rows)})
    return True
```

Comando en `cli.py`:

```python
def cmd_digest(args) -> int:
    from .delivery import digest

    return 0 if digest.send_daily() else 1
```

`railway.digest.json`:

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "startCommand": "handoff digest",
    "cronSchedule": "0 13 * * *",
    "numReplicas": 1,
    "restartPolicyType": "NEVER"
  }
}
```

- [ ] **Step 4: Ejecutar y ver pasar**

Run: `uv run pytest tests/test_digest.py tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/handoff_agent/delivery/digest.py src/handoff_agent/cli.py src/handoff_agent/config.py railway.digest.json tests/test_digest.py tests/test_cli.py .env.example
git commit -m "feat: email diario de resumen por Resend, como servicio programado"
```

---

### Task 9: El receptor de los botones

**Files:**
- Create: `src/handoff_agent/web/__init__.py`, `src/handoff_agent/web/slack_signature.py`, `src/handoff_agent/web/app.py`
- Create: `railway.web.json`
- Modify: `pyproject.toml` (`fastapi`, `uvicorn`)
- Test: `tests/test_slack_signature.py`, `tests/test_web_buttons.py`

**Interfaces:**
- Consumes: `slack_writer.update_card`, `prospects`, `queue.enqueue`.
- Produces: servicio FastAPI con `POST /slack/acciones`; `slack_signature.verify(secret, timestamp, signature, body) -> bool`.

- [ ] **Step 1: Escribir el test de la firma**

```python
# tests/test_slack_signature.py
import hashlib
import hmac
import time

from handoff_agent.web import slack_signature

SECRET = "s3cr3t"


def sign(body: str, timestamp: str) -> str:
    base = f"v0:{timestamp}:{body}".encode()
    return "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()


def test_a_correct_signature_passes():
    ts = str(int(time.time()))
    body = "payload=%7B%7D"
    assert slack_signature.verify(SECRET, ts, sign(body, ts), body) is True


def test_a_forged_signature_fails():
    ts = str(int(time.time()))
    assert slack_signature.verify(SECRET, ts, "v0=deadbeef", "payload=%7B%7D") is False


def test_an_old_request_is_refused():
    """Sin la ventana temporal, cualquiera podría repetir una petición robada."""
    old = str(int(time.time()) - 60 * 10)
    assert slack_signature.verify(SECRET, old, sign("x", old), "x") is False
```

- [ ] **Step 2: Escribir el test del receptor**

```python
# tests/test_web_buttons.py
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from handoff_agent import db
from handoff_agent.web import app as web_app
from tests.test_deliver import a_person

SECRET = "s3cr3t"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setattr(web_app.slack_writer, "update_card", lambda *a, **k: None)
    return TestClient(web_app.app)


def press(client, action, prospect_id):
    payload = json.dumps({
        "type": "block_actions",
        "user": {"username": "anthony"},
        "channel": {"id": "CHANDOFF"},
        "message": {"ts": "1.1", "blocks": []},
        "actions": [{"action_id": action, "value": str(prospect_id)}],
    })
    body = "payload=" + payload
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return client.post("/slack/acciones", content=body,
                       headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                                "Content-Type": "application/x-www-form-urlencoded"})


def test_contactado_moves_the_person(conn, client):
    person = a_person()
    assert press(client, "contactado", person["id"]).status_code == 200
    assert db.fetch_one("select state from prospects where id = %s", (person["id"],))["state"] == "contactado"


def test_descartar_is_sticky_and_stops_alerts(conn, client):
    person = a_person()
    press(client, "descartar", person["id"])
    assert db.fetch_one("select state from prospects where id = %s", (person["id"],))["state"] == "descartado"


def test_investigar_mas_queues_a_new_job(conn, client):
    person = a_person()
    press(client, "investigar_mas", person["id"])
    row = db.fetch_one("select reason, status from research_jobs where slack_user_id = 'U1'")
    assert (row["reason"], row["status"]) == ("manual", "pendiente")


def test_an_unsigned_request_is_rejected(conn, client):
    person = a_person()
    response = client.post("/slack/acciones", content="payload=%7B%7D",
                           headers={"X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=bad"})
    assert response.status_code == 401


def test_every_click_is_recorded_with_who_pressed_it(conn, client):
    person = a_person()
    press(client, "contactado", person["id"])
    row = db.fetch_one("select action, payload from agent_actions where action = 'boton_pulsado'")
    assert row["payload"]["usuario"] == "anthony"
```

- [ ] **Step 3: Ejecutar y ver fallar**

Run: `uv run pytest tests/test_slack_signature.py tests/test_web_buttons.py -v`
Expected: FAIL, `ModuleNotFoundError: handoff_agent.web`

- [ ] **Step 4: Implementar la verificación de firma**

```python
# src/handoff_agent/web/slack_signature.py
"""Verificación de la firma de Slack.

Sin esto, cualquiera que conozca la URL puede descartar prospectos. La ventana
de 5 minutos es lo que impide repetir una petición interceptada.
"""

from __future__ import annotations

import hashlib
import hmac
import time

MAX_AGE_SECONDS = 60 * 5


def verify(secret: str, timestamp: str, signature: str, body: str) -> bool:
    try:
        age = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > MAX_AGE_SECONDS:
        return False
    expected = "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:{body}".encode(),
                                hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
```

- [ ] **Step 5: Implementar el receptor**

```python
# src/handoff_agent/web/app.py
"""Recibe los clics de la tarjeta. Es lo único del sistema expuesto a internet.

Slack corta a los 3 segundos, así que cada acción es una escritura corta en la
base y una actualización de la tarjeta. Nada de research aquí: "Investigar más"
encola y se va.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, Response

from .. import db, ledger
from ..config import load_settings
from ..delivery import slack_writer
from ..ingest import queue
from . import slack_signature

logger = logging.getLogger(__name__)
app = FastAPI(title="handoff-acciones")

NEW_STATE = {"contactado": "contactado", "descartar": "descartado"}


@app.get("/salud")
def salud() -> dict:
    return {"ok": True}


@app.post("/slack/acciones")
async def acciones(request: Request) -> Response:
    settings = load_settings()
    body = (await request.body()).decode()
    if not settings.slack_signing_secret or not slack_signature.verify(
        settings.slack_signing_secret,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
        body,
    ):
        return Response(status_code=401)

    payload = json.loads(parse_qs(body)["payload"][0])
    action = payload["actions"][0]
    prospect_id, action_id = action["value"], action["action_id"]
    who = payload.get("user", {}).get("username", "desconocido")

    person = db.fetch_one("select * from prospects where id = %s", (prospect_id,))
    if person is None:
        return Response(status_code=200)

    if action_id in NEW_STATE:
        db.execute("update prospects set state = %s, updated_at = now() where id = %s",
                   (NEW_STATE[action_id], prospect_id))
    elif action_id == "investigar_mas":
        queue.enqueue(person["slack_user_id"], "manual")

    ledger.record_action("boton_pulsado", {"accion": action_id, "usuario": who},
                         prospect_id=prospect_id)

    note = {"contactado": "✅ Marcado como contactado",
            "descartar": "🚫 Descartado: no volverá a aparecer",
            "investigar_mas": "🔁 En cola para volver a investigar"}[action_id]
    blocks = payload["message"]["blocks"]
    blocks = [b for b in blocks if b.get("type") != "actions"]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"{note} · por @{who}"}]})
    try:
        slack_writer.update_card(payload["channel"]["id"], payload["message"]["ts"], blocks, note)
    except Exception:  # noqa: BLE001 - el estado ya cambió, que no se pierda por el repintado
        logger.exception("no se pudo actualizar la tarjeta")
    return Response(status_code=200)
```

`railway.web.json`:

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "startCommand": "uvicorn handoff_agent.web.app:app --host :: --port $PORT",
    "numReplicas": 1,
    "healthcheckPath": "/salud",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

- [ ] **Step 6: Ejecutar y ver pasar**

Run: `uv run pytest tests/test_slack_signature.py tests/test_web_buttons.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/handoff_agent/web railway.web.json pyproject.toml uv.lock tests/test_slack_signature.py tests/test_web_buttons.py
git commit -m "feat: receptor de los botones con firma verificada"
```

---

### Task 10: Prueba de extremo a extremo y documentación

**Files:**
- Create: `tests/test_entrega_e2e.py`
- Modify: `README.md`

- [ ] **Step 1: Escribir la prueba**

Un mensaje en el canal falso recorre watcher → resolver → research (con OpenAI y herramientas falseadas, como en `tests/test_ingest_e2e.py`) → tarjeta publicada en el bot falso → botón "Descartar" por el receptor → la persona queda descartada y una segunda entrega no publica nada.

- [ ] **Step 2: Ejecutarla**

Run: `uv run pytest tests/test_entrega_e2e.py -v`
Expected: PASS

- [ ] **Step 3: Documentar en el README**

Sección "Entrega (Plan 3)": los tres servicios de Railway (worker, receptor, resumen diario), las variables nuevas, cómo crear la app de Slack de Handoff (scopes de bot `chat:write`, Interactivity con la URL del receptor), y los límites conocidos.

- [ ] **Step 4: Suite, ruff y commit**

```bash
uv run pytest -q
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
git add tests/test_entrega_e2e.py README.md
git commit -m "test: entrega de extremo a extremo y README del Plan 3"
```

---

## Definición de terminado

- `uv run pytest` en verde contra `handoff_test`.
- Un mensaje de prueba produce una tarjeta real en el canal de Handoff, con enlace al original.
- Los tres botones cambian la base y repintan la tarjeta.
- Un descartado no vuelve a generar tarjeta, SMS ni línea de email.
- El SMS respeta el tope de 3 y el horario; cada envío está en `cost_events`.
- El email diario llega con señales bajas, miembros nuevos y gasto.

## Validación que queda para cuando haya credenciales

- App de Slack de Handoff instalada, con el canal creado y la URL del receptor configurada.
- Número de Twilio y teléfono de Anthony.
- Dominio verificado en Resend.
