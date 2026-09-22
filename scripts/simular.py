"""Ejercita el pipeline entero desde la terminal, sin depender del Slack ajeno
del Founders Club (la app lectora todavía no está instalada ahí, y crear
cuentas de Slack solo para probar no es una opción).

Cada subcomando dispara el mismo código de producción que corre en real —
`ingest.resolver`, `ingest.research_runner.run_next_job`, `delivery.sms`,
`delivery.digest`, el receptor de botones de `web/app.py` — nunca una copia
simplificada. Lo único que cambia es de dónde sale el "perfil de Slack": en
vez de `FoundersClubReader` (que sí llama a Slack), `OfflineReader` da la
misma superficie (`owner_id`, `history`, `members`, `user_profile`,
`permalink`) a partir de lo que ya hay en la base y de lo que se pasó por
línea de comandos. Nunca importa `slack_sdk`.

Ver `docs/pruebas-manuales.md` para el paso a paso de cada flujo, qué prueba,
qué cuesta y cómo limpiar después.

Uso (DATABASE_URL tiene que ser la de Supabase Cloud, como el resto de
scripts de esta carpeta; con `--local` acepta cualquier otra, pero entonces
hace falta el stack local -- `supabase start` -- arriba):

    uv run python scripts/simular.py mensaje "Ada Ruiz" "Acme" "hola, buscamos soporte"
    uv run python scripts/simular.py cola
    uv run python scripts/simular.py boton contactado <prospect_id>
    uv run python scripts/simular.py sms <prospect_id>
    uv run python scripts/simular.py digest --ver
    uv run python scripts/simular.py estado <prospect_id>
    uv run python scripts/simular.py limpiar
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import sys
import time
from datetime import UTC, date, datetime

import httpx

from handoff_agent import db
from handoff_agent.config import load_settings
from handoff_agent.delivery import bands, digest, sms
from handoff_agent.ingest import research_runner, resolver
from handoff_agent.slack_client import SlackUserNotFound, UserProfile
from handoff_agent.tools import prospects

# "USIM" nunca choca con un id real de Slack (empieza por "U" seguido de
# letras mayúsculas/dígitos en base32, nunca de otra "S") ni con "UPRUEBA..."
# de tarjeta_prueba.py -- limpiar() borra ambos prefijos por separado.
FAKE_ID_PREFIX = "USIM"
DEFAULT_CHANNEL = "CSIMULADO"
# Slack ts real cuando la persona nunca tuvo tarjeta de verdad: solo hace
# falta que _repaint() de web/app.py tenga algo que ignorar si falla.
DUMMY_TS = "1700000000.000000"
SIMULATED_USER = {"id": "USIMULADOR", "username": "simulador"}
MAX_JOB_ITERATIONS = 5

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def fake_user_id(full_name: str, company: str) -> str:
    """Id de Slack determinista para una persona simulada: el mismo nombre y
    empresa siempre caen en el mismo slack_user_id, así repetir el mismo
    `mensaje` reproduce el camino de "ya hay dossier vigente" en vez de crear
    a alguien nuevo cada vez. El separador nulo evita que ("AB", "C") y
    ("A", "BC") caigan en el mismo hash."""
    digest_hex = hashlib.sha1(f"{full_name}\x00{company}".encode()).hexdigest()[:8]
    return f"{FAKE_ID_PREFIX}{digest_hex}"


class OfflineReader:
    """Mismo surface que `FoundersClubReader` (y que `tests/slack_fakes.py`),
    para que `ingest.research_runner.run_next_job` y `delivery.deliver_for`
    corran sin cambios y sin que exista ningún Slack detrás.

    `user_profile` responde con el perfil que se le haya fijado a mano
    (`set_profile`, con lo que trajo la línea de comandos) o, si no hay uno,
    con lo que ya hay guardado en `prospects` -- así `cola`, que no recibe
    ningún argumento nuevo, también puede drenar la cola.
    """

    def __init__(self, owner_id: str = "USIMOWNER") -> None:
        self._owner_id = owner_id
        self._overrides: dict[str, UserProfile] = {}
        self._history: dict[str, list[dict]] = {}
        self._members: dict[str, set[str]] = {}

    def set_profile(
        self,
        user_id: str,
        *,
        full_name: str | None,
        company_name: str | None,
        email: str | None,
    ) -> None:
        # "CEO @ Empresa" es el mismo formato que profile_hints.company_from_title
        # ya sabe leer: así el research recibe la empresa por el mismo camino
        # que un perfil real de Slack, en vez de un atajo propio del script.
        title = f"CEO @ {company_name}" if company_name else ""
        self._overrides[user_id] = UserProfile(
            user_id=user_id,
            real_name=full_name or "",
            title=title,
            email=email or "",
            is_bot=False,
            deleted=False,
        )

    def add_message(self, channel: str, message: dict) -> None:
        self._history.setdefault(channel, []).append(message)

    def set_members(self, channel: str, *user_ids: str) -> None:
        self._members[channel] = set(user_ids)

    # --- la misma superficie que FoundersClubReader ---

    def owner_id(self) -> str:
        return self._owner_id

    def history(self, channel: str, oldest: str) -> list[dict]:
        return [m for m in self._history.get(channel, []) if float(m["ts"]) > float(oldest)]

    def members(self, channel: str) -> set[str]:
        return set(self._members.get(channel, set()))

    def user_profile(self, user_id: str) -> UserProfile:
        if user_id in self._overrides:
            return self._overrides[user_id]
        row = db.fetch_one(
            "select full_name, company_name from prospects where slack_user_id = %s", (user_id,)
        )
        if row is None:
            raise SlackUserNotFound(f"users_info simulado: {user_id} no existe")
        title = f"CEO @ {row['company_name']}" if row["company_name"] else ""
        return UserProfile(
            user_id=user_id,
            real_name=row["full_name"] or "",
            title=title,
            email="",
            is_bot=False,
            deleted=False,
        )

    def permalink(self, channel: str, ts: str) -> str:
        return f"https://simulado.slack.test/archives/{channel}/p{ts.replace('.', '')}"


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Firma v0 de Slack -- exactamente el cálculo que `slack_signature.verify`
    espera (ver `web/slack_signature.py`), para que el receptor real acepte
    esta petición como si viniera de Slack de verdad."""
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def build_block_actions_payload(action_id: str, prospect_id: str, channel: str, ts: str) -> dict:
    """El mismo payload que Slack manda al pulsar un botón de la tarjeta (ver
    `web/app.py:_handle`): el canal decide si se acepta, el `action_id` y el
    `value` deciden el efecto, y `message.ts` solo importa para repintar --
    uno inventado no rompe nada si la persona nunca tuvo tarjeta real."""
    return {
        "type": "block_actions",
        "user": dict(SIMULATED_USER),
        "channel": {"id": channel},
        "actions": [{"action_id": action_id, "value": str(prospect_id)}],
        "message": {"ts": ts, "blocks": []},
    }


def _validate_domain(domain: str) -> None:
    if not _DOMAIN_RE.match(domain.strip().lower()):
        raise SystemExit(f"--web no parece un dominio válido: {domain!r} (ej. acme.com)")


def _gate(args: argparse.Namespace) -> None:
    """Corta si DATABASE_URL no es Supabase Cloud y no se pasó --local, y
    avisa fuerte cuando sí es Cloud: estos comandos investigan de verdad,
    mandan SMS de verdad y publican tarjetas de verdad. --local no exime de
    tener el stack local (`supabase start`) levantado, solo relaja qué URL se
    acepta."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("Falta DATABASE_URL en el entorno.")
    if "supabase.com" in url:
        host = url.split("@")[-1].split("/")[0]
        print(f"AVISO: {host} es Supabase Cloud -- esto escribe en producción y gasta dinero real.")
    elif not args.local:
        raise SystemExit(
            "DATABASE_URL no parece Supabase Cloud. Si es la base local (necesita "
            "'supabase start' levantado), repite el comando con --local."
        )


def _print_job_result(result: research_runner.RunResult, prospect_id: str | None) -> None:
    if result.status == "limitado":
        print("límite horario de research alcanzado (research_por_hora); se para aquí.")
        return
    detail = f" ({result.detail})" if result.detail else ""
    print(f"job reclamado para {result.slack_user_id}: {result.status}{detail}")
    if result.status != "hecho" or prospect_id is None:
        return

    job = db.fetch_one(
        "select outcome from research_jobs where slack_user_id = %s and status = 'hecho' "
        "order by finished_at desc limit 1",
        (result.slack_user_id,),
    )
    outcome = (job["outcome"] if job else None) or {}
    print(f"  research: {outcome.get('estado', '—')} · coste ${outcome.get('coste_usd', '0')}")
    if outcome.get("motivo"):
        print(f"  motivo: {outcome['motivo']}")

    dossier = db.fetch_one(
        "select content from dossiers where prospect_id = %s order by version desc limit 1",
        (prospect_id,),
    )
    band = bands.band_for(dossier["content"]) if dossier else None
    print(f"  banda: {band or '—'}")

    card = db.fetch_one(
        "select message_ts from deliveries where prospect_id = %s and kind = 'slack' "
        "order by created_at desc limit 1",
        (prospect_id,),
    )
    posted = bool(card and card["message_ts"])
    print(f"  tarjeta publicada: {'sí' if posted else 'no'}")


def cmd_mensaje(args: argparse.Namespace) -> int:
    _gate(args)
    user_id = fake_user_id(args.full_name, args.company)
    person = prospects.upsert_prospect(user_id, full_name=args.full_name)
    db.execute(
        "update prospects set company_name = coalesce(%s, company_name), updated_at = now() "
        "where id = %s",
        (args.company, person["id"]),
    )
    if args.web:
        _validate_domain(args.web)
        db.execute(
            "update prospects set company_domain_override = %s, updated_at = now() where id = %s",
            (args.web.strip().lower(), person["id"]),
        )

    ts = f"{time.time():.6f}"
    db.execute(
        "insert into slack_messages (channel_id, ts, user_id, text, status) "
        "values (%s, %s, %s, %s, 'nuevo')",
        (args.canal, ts, user_id, args.message),
    )
    print(f"mensaje guardado: {args.canal}/{ts} de {user_id}")

    resolved = resolver.resolve_pending()
    print(
        f"resolver: encolados {resolved.enqueued} · archivados {resolved.archived} · "
        f"a scoring {resolved.to_scoring} · con error {resolved.failed}"
    )

    reader = OfflineReader()
    reader.set_profile(
        user_id, full_name=args.full_name, company_name=args.company, email=args.email
    )

    for _ in range(MAX_JOB_ITERATIONS):
        result = research_runner.run_next_job(reader)
        if result is None:
            print("cola de research vacía.")
            break
        _print_job_result(result, person["id"])
        if result.status == "limitado":
            break
    else:
        print(f"tope de {MAX_JOB_ITERATIONS} vueltas alcanzado; usa 'cola' para seguir drenando.")

    print(f"prospect_id: {person['id']}")
    settings = load_settings()
    if settings.panel_url:
        print(f"panel: {settings.panel_url}/personas/{person['id']}")
    return 0


def cmd_cola(args: argparse.Namespace) -> int:
    _gate(args)
    reader = OfflineReader()
    processed = 0
    for _ in range(args.max):
        result = research_runner.run_next_job(reader)
        if result is None:
            print("cola vacía.")
            break
        prospect = None
        if result.slack_user_id:
            prospect = db.fetch_one(
                "select id from prospects where slack_user_id = %s", (result.slack_user_id,)
            )
        _print_job_result(result, prospect["id"] if prospect else None)
        processed += 1
        if result.status == "limitado":
            break
    else:
        print(f"se alcanzó --max ({args.max}); puede quedar trabajo en la cola.")
    print(f"tareas procesadas: {processed}")
    return 0


def cmd_boton(args: argparse.Namespace) -> int:
    _gate(args)
    settings = load_settings()
    if not settings.slack_signing_secret:
        raise SystemExit("Falta SLACK_SIGNING_SECRET (tiene que ser el mismo que usa el receptor).")

    person = db.fetch_one("select state from prospects where id = %s", (args.prospect_id,))
    if person is None:
        raise SystemExit(f"No existe la persona {args.prospect_id}.")
    print(f"estado antes: {person['state']}")

    card = db.fetch_one(
        "select channel_id, message_ts from deliveries where prospect_id = %s and kind = 'slack' "
        "order by created_at desc limit 1",
        (args.prospect_id,),
    )
    if card and card["channel_id"] and card["message_ts"]:
        channel, ts = card["channel_id"], card["message_ts"]
    else:
        channel = settings.handoff_channel_id
        if not channel:
            raise SystemExit(
                "Falta HANDOFF_SLACK_CHANNEL_ID, y esta persona no tiene ninguna tarjeta real."
            )
        ts = DUMMY_TS

    payload = build_block_actions_payload(args.accion, args.prospect_id, channel, ts)
    body = ("payload=" + json.dumps(payload)).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": (
            "v0=firma-invalida"
            if args.sin_firma
            else sign(settings.slack_signing_secret, timestamp, body)
        ),
    }

    response = httpx.post(f"{args.url}/slack/acciones", content=body, headers=headers, timeout=10)
    print(f"HTTP {response.status_code}")

    after = db.fetch_one("select state from prospects where id = %s", (args.prospect_id,))
    print(f"estado después: {after['state'] if after else '—'}")
    return 0


def cmd_sms(args: argparse.Namespace) -> int:
    _gate(args)
    sent = sms.maybe_send(args.prospect_id, "alta")
    if sent:
        row = db.fetch_one(
            "select external_id from deliveries where prospect_id = %s and kind = 'sms' "
            "order by created_at desc limit 1",
            (args.prospect_id,),
        )
        print(f"enviado (sid {row['external_id'] if row else '?'})")
        return 0

    stopped = db.fetch_one(
        "select action, payload from agent_actions where prospect_id = %s "
        "and action in ('sms_detenido', 'sms_fallido') order by created_at desc limit 1",
        (args.prospect_id,),
    )
    if stopped:
        print(f"no enviado: {stopped['action']} — {stopped['payload']}")
        return 0

    settings = load_settings()
    configured = all(
        (
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            settings.twilio_from,
            settings.twilio_to,
        )
    )
    if not configured:
        print("no enviado: faltan variables TWILIO_* (SMS sin configurar).")
        return 0

    sent_today = db.fetch_one(
        "select count(*) as n from deliveries where kind = 'sms' and created_at >= current_date"
    )["n"]
    print(
        "no enviado: probablemente fuera de horario (sms_hora_inicio/sms_hora_fin) o tope diario "
        f"alcanzado (sms_por_dia). SMS ya enviados hoy: {sent_today}."
    )
    return 0


def _html_to_text(body: str) -> str:
    """Conversión mínima para previsualizar en la terminal: no hace falta un
    parser HTML de verdad para un resumen de tres párrafos y una lista."""
    text = re.sub(r"<li>", "\n- ", body)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def cmd_digest(args: argparse.Namespace) -> int:
    _gate(args)
    day = date.fromisoformat(args.dia) if args.dia else digest._default_day(datetime.now(UTC))
    if args.ver:
        subject, body = digest.build(day)
        print(f"asunto: {subject}")
        print(_html_to_text(body))
        return 0
    status = digest.send_daily(day=day)
    print(status)
    return 1 if status == "fallido" else 0


def cmd_estado(args: argparse.Namespace) -> int:
    _gate(args)
    person = db.fetch_one("select * from prospects where id = %s", (args.prospect_id,))
    if person is None:
        print("no existe esa persona.")
        return 1

    print(f"estado: {person['state']}")
    print(f"nombre: {person['full_name'] or '—'}  ·  empresa: {person['company_name'] or '—'}")

    dossier = db.fetch_one(
        "select version, content from dossiers where prospect_id = %s order by version desc limit 1",
        (args.prospect_id,),
    )
    if dossier:
        content = dossier["content"]
        score = (content.get("encaje_handoff") or {}).get("puntuacion")
        band = bands.band_for(content)
        print(f"dossier: v{dossier['version']}  ·  encaje {score}/3  ·  banda {band or '—'}")
        print(f"empresa_no_confirmada: {'sí' if content.get('empresa_no_confirmada') else 'no'}")
        print(f"datos_proveedor: {'sí' if content.get('datos_proveedor') else 'no'}")
    else:
        print("sin dossier todavía.")

    print("overrides del panel:")
    print(f"  empresa: {person['company_name_override'] or '—'}")
    print(f"  web: {person['company_domain_override'] or '—'}")
    print(f"  enlaces: {person['research_links'] or '—'}")
    print(f"  notas: {person['research_notes'] or '—'}")

    delivered = db.fetch_all(
        "select kind, band, created_at from deliveries where prospect_id = %s "
        "order by created_at desc",
        (args.prospect_id,),
    )
    print(f"entregas: {len(delivered)}")
    for row in delivered:
        print(f"  {row['kind']} · {row['band']} · {row['created_at']}")

    open_jobs = db.fetch_all(
        "select status, reason from research_jobs where slack_user_id = %s "
        "and status in ('pendiente', 'en_curso')",
        (person["slack_user_id"],),
    )
    print(f"tareas de research abiertas: {len(open_jobs)}")

    spend = db.fetch_one(
        "select coalesce((select sum(cost_usd) from llm_calls where prospect_id = %s), 0) "
        "+ coalesce((select sum(cost_usd) from cost_events where prospect_id = %s), 0) as total",
        (args.prospect_id, args.prospect_id),
    )["total"]
    print(f"gasto en esta persona: ${spend:.4f}")
    return 0


def cmd_limpiar(args: argparse.Namespace) -> int:
    _gate(args)
    if not args.si:
        answer = input('Escribe "borrar" para confirmar (borra a todas las personas simuladas): ')
        if answer.strip() != "borrar":
            print("cancelado.")
            return 1

    people = db.execute(
        "delete from prospects where slack_user_id like %s or slack_user_id like %s",
        (f"{FAKE_ID_PREFIX}%", "UPRUEBA%"),
    )
    # slack_messages.user_id no es una clave ajena (ver 0004_slack_ingest.sql):
    # el cascade de prospects no toca esta tabla, hay que borrarla aparte.
    messages = db.execute(
        "delete from slack_messages where user_id like %s or user_id like %s",
        (f"{FAKE_ID_PREFIX}%", "UPRUEBA%"),
    )
    print(f"borrados: {people} persona(s), {messages} mensaje(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--local",
        action="store_true",
        help="acepta cualquier DATABASE_URL (stack local con 'supabase start'), no solo Cloud",
    )

    parser = argparse.ArgumentParser(
        prog="simular.py", description="Ejercita el pipeline sin el Slack del Founders Club."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    mensaje = sub.add_parser(
        "mensaje", parents=[common], help="simula un mensaje entrante y corre todo el pipeline"
    )
    mensaje.add_argument("full_name")
    mensaje.add_argument("company")
    mensaje.add_argument("message")
    mensaje.add_argument("--email", help="correo de trabajo: prueba el camino de dominio confiado")
    mensaje.add_argument(
        "--web", help="dominio ya confiado (como panel_corregir_web), sin adivinar"
    )
    mensaje.add_argument("--canal", default=DEFAULT_CHANNEL)
    mensaje.set_defaults(func=cmd_mensaje)

    cola = sub.add_parser("cola", parents=[common], help="drena la cola de research pendiente")
    cola.add_argument("--max", type=int, default=20)
    cola.set_defaults(func=cmd_cola)

    boton = sub.add_parser(
        "boton", parents=[common], help="manda un block_actions firmado al receptor"
    )
    boton.add_argument("accion", choices=["contactado", "descartar", "investigar_mas"])
    boton.add_argument("prospect_id")
    boton.add_argument("--url", default="http://127.0.0.1:8000")
    boton.add_argument(
        "--sin-firma", dest="sin_firma", action="store_true", help="prueba que se rechaza con 401"
    )
    boton.set_defaults(func=cmd_boton)

    sms_cmd = sub.add_parser("sms", parents=[common], help="prueba delivery.sms.maybe_send")
    sms_cmd.add_argument("prospect_id")
    sms_cmd.set_defaults(func=cmd_sms)

    digest_cmd = sub.add_parser("digest", parents=[common], help="manda o previsualiza el resumen")
    digest_cmd.add_argument("--dia", help="YYYY-MM-DD; por defecto, el día que tocaría hoy")
    digest_cmd.add_argument("--ver", action="store_true", help="solo muestra asunto y cuerpo")
    digest_cmd.set_defaults(func=cmd_digest)

    estado_cmd = sub.add_parser("estado", parents=[common], help="resumen de una persona")
    estado_cmd.add_argument("prospect_id")
    estado_cmd.set_defaults(func=cmd_estado)

    limpiar_cmd = sub.add_parser("limpiar", parents=[common], help="borra los datos simulados")
    limpiar_cmd.add_argument("--si", action="store_true", help="no pide confirmación")
    limpiar_cmd.set_defaults(func=cmd_limpiar)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
