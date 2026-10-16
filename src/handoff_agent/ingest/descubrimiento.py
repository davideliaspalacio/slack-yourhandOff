"""Lo que el modo automático de canales ESCRIBE (migración 0028).

Vive aquí y no en `canales.py` por una razón que no es de estilo: `canales.py` lo importa
el servidor de lectura del MCP, y un test comprueba que ese módulo no contenga una sola
sentencia de escritura. Así, por mucho que alguien se equivoque más arriba, una
herramienta de lectura no tiene a mano ninguna función que escriba. Quien escribe es el
worker, y el worker es quien importa esto.

Los umbrales, la escalera del retroceso y los lectores (`modo_todos`, `por_sondear`,
`toca_miembros`, `vigilados_automaticos`) siguen en `canales.py`.
"""

from __future__ import annotations

from .. import canales, db


def guardar_descubrimiento(canales_slack: list[dict], tarjetas: str | None = None) -> dict:
    """Guarda lo que Slack dice que hay: nombre y si la cuenta lectora está dentro.

    `canales_slack` es lo que devuelve `FoundersClubReader.canales()`. Se guardan todos,
    dentro o fuera, para poder enseñar la lista completa a quien elige; sondear es otra
    cosa y la decide `canales.por_sondear`, que exige `es_miembro`.

    El canal donde el bot publica las tarjetas nunca se marca como miembro, por si
    alguna vez viviera en el mismo workspace: el sistema se niega a publicar en un canal
    que vigila, y esto lo deja fuera de la vigilancia antes de que la cuestión aparezca.

    Devuelve el recuento, para el log.
    """
    tope = (tarjetas or "").strip().upper() or None
    dentro = 0
    for canal in canales_slack:
        channel_id = (canal.get("id") or "").strip().upper()
        if not canales.CODIGO.match(channel_id):
            continue
        es_miembro = bool(canal.get("is_member")) and channel_id != tope
        dentro += 1 if es_miembro else 0
        nombre = (canal.get("name") or "").strip() or None
        db.execute(
            """
            insert into canales (channel_id, nombre, es_miembro, visto_at, actualizado_at)
            values (%s, %s, %s, now(), now())
            on conflict (channel_id) do update
                set nombre = coalesce(excluded.nombre, canales.nombre),
                    es_miembro = excluded.es_miembro,
                    visto_at = excluded.visto_at
            """,
            (channel_id, nombre, es_miembro),
        )
    return {"vistos": len(canales_slack), "dentro": dentro}


def marcar_sondeado(channel_id: str, mensajes_nuevos: int) -> None:
    """Apunta que se leyó, y cuándo le toca otra vez.

    Con mensajes, el contador de vueltas vacías vuelve a cero y el canal cae en el
    primer escalón de `canales.ESCALERA_MINUTOS`. Sin mensajes, sube un escalón hasta
    quedarse en el último.
    """
    db.execute(
        """
        update canales set
            sondeado_at = now(),
            vueltas_vacias = case when %(nuevos)s > 0 then 0 else vueltas_vacias + 1 end,
            mensajes_vistos = mensajes_vistos + %(nuevos)s,
            siguiente_sondeo_at = now() + make_interval(mins =>
                case when %(nuevos)s > 0 then %(primero)s
                     else %(escalera)s[least(vueltas_vacias + 2, %(largo)s)]
                end)
         where channel_id = %(canal)s
        """,
        {
            "canal": channel_id,
            "nuevos": mensajes_nuevos,
            "primero": canales.ESCALERA_MINUTOS[0],
            # El array de Postgres cuenta desde 1: `vueltas_vacias + 2` es el escalón
            # siguiente al que acaba de gastarse, acotado al último.
            "escalera": list(canales.ESCALERA_MINUTOS),
            "largo": len(canales.ESCALERA_MINUTOS),
        },
    )


def marcar_miembros(channel_id: str) -> None:
    """Apunta que se revisó su lista de miembros, para no volver en horas."""
    db.execute(
        "update canales set miembros_revisados_at = now() where channel_id = %s", (channel_id,)
    )
