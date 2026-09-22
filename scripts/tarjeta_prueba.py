"""Publica una tarjeta de prueba en el canal de Handoff, por el mismo camino
que el worker (deliver_for), para probar el bot y los botones de punta a punta.

Crea (o reutiliza) una persona ficticia, "Prueba Handoff" (slack_user_id
UPRUEBA0001), le guarda un dossier nuevo de banda alta y entrega su tarjeta.
Cada ejecución guarda una versión nueva, así que siempre sale una tarjeta.
Los botones actúan sobre esa persona en la base real: Descartar la deja
descartada, y esta misma ejecución la devuelve a "investigado".

Uso (DATABASE_URL tiene que ser la de Supabase Cloud, la misma del receptor):

    DATABASE_URL=... HANDOFF_SLACK_BOT_TOKEN=... HANDOFF_SLACK_CHANNEL_ID=... \\
        uv run python scripts/tarjeta_prueba.py
"""

from __future__ import annotations

import os
import sys

from handoff_agent import db
from handoff_agent.delivery import deliver
from handoff_agent.tools import prospects

SLACK_USER_ID = "UPRUEBA0001"

DOSSIER = {
    "persona": {"nombre": "Prueba Handoff", "cargo": "CEO (tarjeta de prueba)"},
    "empresa": {"nombre": "Acme (prueba)", "empleados_aprox": 120, "sector": "SaaS"},
    "contratacion": {
        "vacantes_abiertas": 6,
        "roles": ["Customer Support Specialist", "Operations Associate"],
        "roles_deslocalizables": ["soporte", "operaciones"],
    },
    "senales_contexto": [
        {"hecho": "Tarjeta de prueba: no es una persona real", "fuente": "https://example.com"}
    ],
    "encaje_handoff": {
        "puntuacion": 3,
        "razon": "Prueba del bot y de los botones: Contactado, Descartar, Investigar más.",
    },
    "resumen": "Tarjeta de prueba del agente. Pulsa los botones para comprobar el receptor.",
    "datos_proveedor": {
        "empleados_linkedin": 120,
        "empleados_por_area": {"support": 25, "operations": 18, "sales": 12},
        "fuente": "proveedor externo (sin verificar)",
    },
}


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if "supabase.com" not in url:
        print("DATABASE_URL tiene que ser la de Supabase Cloud (la misma que usa el receptor).")
        return 1
    for name in ("HANDOFF_SLACK_BOT_TOKEN", "HANDOFF_SLACK_CHANNEL_ID"):
        if not os.environ.get(name):
            print(f"Falta {name}.")
            return 1

    person = prospects.upsert_prospect(SLACK_USER_ID, full_name="Prueba Handoff")
    db.execute(
        "update prospects set state = 'investigado', company_name = 'Acme (prueba)', "
        "updated_at = now() where id = %s",
        (person["id"],),
    )
    version = prospects.save_dossier(
        person["id"], DOSSIER, [{"url": "https://example.com", "kind": "home", "title": "Prueba"}]
    )

    # Sin mensajes de esta persona, deliver_for no pide permalink: no hace
    # falta el lector del Founders Club.
    band = deliver.deliver_for(str(person["id"]), reader=None)
    card = db.fetch_one(
        "select channel_id, message_ts from deliveries "
        "where prospect_id = %s and kind = 'slack' and dossier_version = %s",
        (person["id"], version),
    )
    if band and card and card["message_ts"]:
        print(f"Tarjeta publicada (banda {band}) en {card['channel_id']}. Prueba los botones.")
        return 0
    print("No salió la tarjeta. Mira agent_actions (entrega_fallida / entrega_detenida).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
