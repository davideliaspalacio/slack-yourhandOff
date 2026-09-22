"""Investiga de verdad a una persona y su empresa y publica su tarjeta en el
canal de Handoff, por el mismo camino que el worker: research_person (buscador,
web de la empresa, LinkedIn, vacantes, webhook de enriquecimiento) y después
deliver_for. Cuesta lo que cuesta un research real (queda en el ledger).

Solo sale tarjeta si el encaje da banda alta o media; si no, lo dice.

Uso (DATABASE_URL de Supabase Cloud; OPENAI_API_KEY y SERPER_API_KEY salen
del .env si no se pasan):

    DATABASE_URL=... HANDOFF_SLACK_BOT_TOKEN=... HANDOFF_SLACK_CHANNEL_ID=... \\
        uv run python scripts/tarjeta_real.py "Nombre Apellido" "Empresa" [dominio.com]
"""

from __future__ import annotations

import os
import sys

from handoff_agent import db
from handoff_agent.delivery import bands, deliver
from handoff_agent.research import worker


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('Uso: tarjeta_real.py "Nombre Apellido" "Empresa" [dominio.com]')
        return 1
    full_name, company = argv[0], argv[1]
    domain = argv[2] if len(argv) > 2 else None

    if "supabase.com" not in os.environ.get("DATABASE_URL", ""):
        print("DATABASE_URL tiene que ser la de Supabase Cloud (la misma que usa el receptor).")
        return 1
    for name in ("HANDOFF_SLACK_BOT_TOKEN", "HANDOFF_SLACK_CHANNEL_ID", "OPENAI_API_KEY"):
        if not os.environ.get(name):
            print(f"Falta {name}.")
            return 1

    # Correr este script es una decisión humana explícita: si la persona estaba
    # descartada (p. ej. al probar el botón Discard), se reactiva para poder
    # investigarla y entregarla otra vez.
    reactivated = db.execute(
        "update prospects set state = 'nuevo', updated_at = now() "
        "where slack_user_id = %s and state = 'descartado'",
        (worker.manual_user_id(full_name, company),),
    )
    if reactivated:
        print("La persona estaba descartada: se reactiva para esta prueba.")

    print(f"Investigando a {full_name} ({company})... puede tardar un par de minutos.")
    # force: si ya había un dossier reciente, se investiga igual.
    outcome = worker.research_person(full_name, company, domain, force=True)
    print(f"Research: {outcome.status}, versión {outcome.version}, coste ${outcome.cost_usd:.4f}")
    if outcome.reason:
        print(f"Motivo: {outcome.reason}")
    if outcome.version is None:
        return 1

    dossier = db.fetch_one(
        "select content from dossiers where prospect_id = %s and version = %s",
        (outcome.prospect_id, outcome.version),
    )["content"]
    score = (dossier.get("encaje_handoff") or {}).get("puntuacion")
    band = bands.band_for(dossier)
    print(f"Encaje {score}/3 -> banda {band}")
    if band not in ("alta", "media"):
        print("Con esa banda no sale tarjeta (va al email diario o a nada).")
        return 0

    deliver.deliver_for(outcome.prospect_id, reader=None)
    card = db.fetch_one(
        "select channel_id, message_ts from deliveries "
        "where prospect_id = %s and kind = 'slack' and dossier_version = %s",
        (outcome.prospect_id, outcome.version),
    )
    if card and card["message_ts"]:
        print(f"Tarjeta publicada en {card['channel_id']}.")
        return 0
    print("No salió la tarjeta. Mira agent_actions (entrega_fallida / entrega_detenida).")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
