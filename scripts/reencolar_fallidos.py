"""Recupera lo que se quedó sin hacer por una causa externa (sin crédito en OpenAI).

Dos cosas distintas, cada una con su interruptor:

`--aplicar` reencola los research fallidos.
`--senales` borra las filas de "sin señal" que el detector escribió cuando el modelo
no respondía, para que vuelva a mirar esos mensajes.

Sobre los research: la cola no los reintenta sola: tras `MAX_ATTEMPTS` intentos `fail()` los deja en
`fallido` con `finished_at`, y `claim_next()` solo reclama `pendiente`. Eso es lo
correcto cuando la culpa es de la persona (un perfil sin datos), pero no cuando la
culpa fue nuestra: la cuenta se quedó sin saldo y el research nunca llegó a correr.

Por defecto solo informa. Con `--aplicar` encola de verdad, pasando por
`queue.enqueue()`, que valida el motivo y respeta el índice de una tarea abierta por
persona: quien ya tenga una tarea viva no se duplica.
"""

from __future__ import annotations

import argparse

from handoff_agent import db
from handoff_agent.ingest import queue
from handoff_agent.signals import procesar

# El fallo que sí merece reintento: el saldo de la cuenta, no los datos de la persona.
CAUSA_EXTERNA = "%credit_balance_exhausted%"

# Cuando empezó el apagón de saldo. Las filas de `message_signals` escritas a partir
# de aquí con `detectada = false` no son clasificaciones: son el detector rindiéndose
# porque el modelo devolvía 429 (`signals/detector.py`, rama `except Exception`).
APAGON_DESDE = "2026-10-14 15:10:00+00"

# Y cuándo terminó: se recargó el saldo y el modelo volvió a responder. El corte de
# arriba sin este de abajo sería una trampa -- a partir de aquí, un `detectada = false`
# es una clasificación de verdad ("este mensaje no pide nada"), y borrarla haría que el
# detector pagase otra vez por el mismo mensaje. Con los dos cortes, correr este script
# de nuevo no puede hacer daño: la ventana del apagón está cerrada.
APAGON_HASTA = "2026-10-16 01:15:00+00"


def candidatos(patron: str) -> list[dict]:
    return db.fetch_all(
        """
        select j.slack_user_id,
               coalesce(p.full_name, '(sin nombre)') as nombre,
               coalesce(p.state::text, '(sin ficha)') as estado,
               count(*) as intentos_fallidos
          from research_jobs j
          left join prospects p on p.slack_user_id = j.slack_user_id
         where j.status = 'fallido'
           and j.last_error ilike %s
           -- nadie con una tarea ya viva: no se pisa el trabajo en marcha
           and not exists (
               select 1 from research_jobs k
                where k.slack_user_id = j.slack_user_id
                  and k.status in ('pendiente', 'en_curso')
           )
         group by j.slack_user_id, p.full_name, p.state
         order by nombre
        """,
        (patron,),
    )


def senales_borrables(desde: str, hasta: str) -> int:
    """Cuántos mensajes volvería a mirar el detector si se limpian sus filas.

    Solo los que siguen dentro de la ventana de edad de una alerta: más viejos, el
    detector ni los lee (`signals/procesar._pendientes`) y la alerta caducaría al
    instante. Esa ventana corre, así que este número baja con las horas."""
    fila = db.fetch_one(
        """
        select count(*) as n
          from message_signals ms
          join slack_messages sm on sm.channel_id = ms.channel_id and sm.ts = ms.ts
         where ms.detectada = false
           and ms.created_at >= %s
           and ms.created_at < %s
           and sm.ts::numeric >= extract(epoch from now() - make_interval(hours => %s))
        """,
        (desde, hasta, procesar.edad_maxima_horas()),
    )
    return fila["n"] if fila else 0


def limpiar_senales(desde: str, hasta: str) -> int:
    """Borra esas filas. Solo `detectada = false`: nunca se toca una señal real."""
    return db.execute(
        """
        delete from message_signals ms
         using slack_messages sm
         where sm.channel_id = ms.channel_id and sm.ts = ms.ts
           and ms.detectada = false
           and ms.created_at >= %s
           and ms.created_at < %s
           and sm.ts::numeric >= extract(epoch from now() - make_interval(hours => %s))
        """,
        (desde, hasta, procesar.edad_maxima_horas()),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="encolar de verdad")
    parser.add_argument("--patron", default=CAUSA_EXTERNA, help="filtro sobre last_error")
    parser.add_argument(
        "--senales",
        action="store_true",
        help="además, dejar que el detector vuelva a mirar los mensajes del apagón",
    )
    parser.add_argument("--desde", default=APAGON_DESDE, help="cuándo empezó el apagón")
    parser.add_argument("--hasta", default=APAGON_HASTA, help="cuándo volvió el saldo")
    args = parser.parse_args()

    if args.senales:
        n = senales_borrables(args.desde, args.hasta)
        horas = procesar.edad_maxima_horas()
        print(f"\nSeñales: {n} mensajes del apagón siguen dentro de la ventana de {horas}h.")
        if not args.aplicar:
            print("  Ensayo. Con --senales --aplicar se limpian y el detector los vuelve a mirar.")
        else:
            print(f"  Limpiadas {limpiar_senales(args.desde, args.hasta)} filas.")
        print()

    gente = candidatos(args.patron)
    if not gente:
        print("No hay research fallidos por esa causa. Nada que hacer.")
        return

    for f in gente:
        print(f"  {f['nombre'][:34]:<34} {f['estado']:<12} {f['slack_user_id']}")

    limite = queue.hourly_limit()
    print(f"\n{len(gente)} personas. El worker respeta su tope de {limite} research/hora,")
    print(f"así que tardará unas {-(-len(gente) // limite)} horas en vaciarse.")

    if not args.aplicar:
        print("\nEnsayo. Para encolar de verdad: --aplicar")
        return

    encoladas = sum(1 for f in gente if queue.enqueue(f["slack_user_id"], "manual"))
    print(f"\nEncoladas {encoladas} de {len(gente)}.")
    if encoladas != len(gente):
        print("Las que faltan ya tenían una tarea abierta; se dejaron como estaban.")


if __name__ == "__main__":
    main()
