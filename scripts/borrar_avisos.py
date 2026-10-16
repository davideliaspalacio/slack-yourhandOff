"""Borra del Slack de Handoff los avisos que el bot publicó por error.

Para qué: al desplegar los avisos de canal por primera vez, el detector miró todo el
histórico de `slack_messages` y anunció como recién llegadas 20 presentaciones de hace
semanas. El corte de edad ya está puesto y no volverá a pasar, pero esos 20 mensajes
siguen en el canal.

Por qué es seguro: NO busca ni adivina. Cada aviso publicado guardó el identificador
exacto del mensaje que Slack devolvió (`eventos_canal.aviso_ts`), así que esto borra
esos y solo esos. No puede llevarse por delante una tarjeta de un lead ni un mensaje de
una persona.

Dónde borra: en el canal de Handoff (`HANDOFF_SLACK_CHANNEL_ID`), con el token del bot
de Handoff. El Founders Club no entra aquí de ninguna forma: ese token no vale en ese
workspace, y el lector del club no tiene un solo método de escritura.

En ensayo por omisión. Con `--aplicar` borra de verdad, y eso no se deshace.
"""

from __future__ import annotations

import argparse
import re

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from handoff_agent import db
from handoff_agent.config import load_settings


def avisos(tipo: str | None) -> list[dict]:
    """Los avisos publicados de los que se guardó el identificador del mensaje."""
    return db.fetch_all(
        """
        select e.id::text as id, e.tipo, e.aviso_ts, e.channel_id, e.ts,
               coalesce(p.full_name, e.slack_user_id, '?') as quien
          from eventos_canal e
          left join prospects p on p.id = e.prospect_id
         where e.estado = 'publicada'
           and e.aviso_ts is not null
           and (%(tipo)s::text is null or e.tipo = %(tipo)s)
         order by e.aviso_ts
        """,
        {"tipo": tipo},
    )


def mensajes_de_seguimiento() -> list[dict]:
    """Los recordatorios y resúmenes publicados, por el apunte del ledger.

    El módulo de seguimiento no guardaba el identificador del mensaje y por eso los dos
    primeros que publicó hubo que borrarlos a mano. Desde entonces lo anota
    (`seguimiento_mensaje`), así que estos también se pueden señalar sin buscarlos."""
    return db.fetch_all(
        """
        select id::text as id,
               payload->>'ts' as aviso_ts,
               'seguimiento ' || coalesce(payload->>'tipo', '?') as tipo,
               '' as quien
          from agent_actions
         where action = 'seguimiento_mensaje'
           and payload->>'ts' is not null
         order by created_at
        """
    )


# Un enlace de Slack: .../archives/C0C68LJQKMK/p1792132316123456 (y a veces ?thread_ts=…).
# La `p` va seguida de la marca de tiempo sin el punto, que es justo lo que pide
# chat.delete. De ahí sale exacta, sin adivinar nada.
ENLACE = re.compile(r"/archives/([CGD][A-Z0-9]+)/p(\d{10})(\d{6})")


def de_enlace(url: str) -> dict:
    """El canal y la marca de tiempo de un enlace copiado con "Copiar el enlace".

    Es la vía para borrar un mensaje del que no guardamos el identificador: en vez de
    calcularlo por la hora -- que borraría OTRO mensaje si falla, y en ese canal hay
    tarjetas de leads reales -- lo da el propio Slack."""
    m = ENLACE.search(url.strip())
    if not m:
        raise SystemExit(
            f"No parece un enlace de mensaje de Slack: {url!r}\n"
            "Se saca con clic derecho en el mensaje -> Copiar el enlace."
        )
    canal, segundos, micros = m.groups()
    return {"id": None, "tipo": "enlace", "quien": canal, "aviso_ts": f"{segundos}.{micros}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="borrar de verdad")
    parser.add_argument(
        "--enlace",
        action="append",
        default=[],
        metavar="URL",
        help="enlace de un mensaje a borrar (clic derecho -> Copiar el enlace). "
        "Se puede repetir. Con esto no se toca nada más.",
    )
    parser.add_argument(
        "--tipo",
        choices=("mencion", "presentacion"),
        help="solo eventos de canal de ese tipo; sin esto entran también los "
        "recordatorios y resúmenes de seguimiento",
    )
    args = parser.parse_args()

    settings = load_settings()
    if not (settings.handoff_bot_token and settings.handoff_channel_id):
        print("Faltan HANDOFF_SLACK_BOT_TOKEN o HANDOFF_SLACK_CHANNEL_ID.")
        return

    if args.enlace:
        # Con enlaces explícitos no se mezcla nada: se borra lo que se pidió y ya.
        lista = [de_enlace(u) for u in args.enlace]
    else:
        lista = avisos(args.tipo)
        if not args.tipo:
            lista += mensajes_de_seguimiento()
    if not lista:
        print("No hay avisos publicados con identificador guardado. Nada que borrar.")
        return

    print(f"Canal de destino: {settings.handoff_channel_id}\n")
    for a in lista:
        print(f"  {a['aviso_ts']:<20} {a['tipo']:<13} {a['quien'][:40]}")
    print(f"\n{len(lista)} mensaje(s).")

    if not args.aplicar:
        print("\nEnsayo: no se ha borrado nada. Para borrar de verdad: --aplicar")
        print("Ojo: borrar un mensaje de Slack NO se puede deshacer.")
        return

    cliente = WebClient(token=settings.handoff_bot_token)
    borrados = 0
    for a in lista:
        try:
            cliente.chat_delete(channel=settings.handoff_channel_id, ts=a["aviso_ts"])
        except SlackApiError as exc:
            error = exc.response.get("error", "")
            if error == "message_not_found":
                # Ya no estaba: alguien lo borró a mano. Se anota igual y se sigue.
                print(f"  {a['aviso_ts']}: ya no existía")
            else:
                print(f"  {a['aviso_ts']}: no se pudo borrar -> {error}")
                continue
        borrados += 1
        if a["id"] is None:
            continue  # vino de un enlace: no hay nada nuestro que apuntar
        if a["tipo"].startswith("seguimiento "):
            # Su apunte vive en el ledger, que es un registro de hechos y no se
            # reescribe: se anota que se borró y se deja de ofrecer.
            db.execute(
                "update agent_actions set payload = payload - 'ts' || "
                "jsonb_build_object('borrado_at', now()) where id = %s",
                (a["id"],),
            )
        else:
            # El aviso deja de contar como publicado: si no, el tope diario seguiría
            # creyendo que el cupo está gastado.
            db.execute(
                "update eventos_canal set estado = 'omitida', motivo = 'aviso borrado', "
                "aviso_ts = null where id = %s",
                (a["id"],),
            )
    print(f"\nBorrados {borrados} de {len(lista)}.")


if __name__ == "__main__":
    main()
