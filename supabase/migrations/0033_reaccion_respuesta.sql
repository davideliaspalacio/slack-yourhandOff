-- Una reacción con emoji es una respuesta: deja de insistir.
--
-- AG lo pidió en el canal: "pls stop alerts if emoji reactions were used for a reply".
-- El rastro de los DMs solo miraba quién escribió el último MENSAJE, así que si él
-- contestaba con un 👍 en vez de teclear, para el agente seguía sin contestar y le
-- recordaba la misma conversación un día tras otro.
--
-- Medido contra el Slack real: de 40 conversaciones, 10 tenían una reacción en su último
-- mensaje. 7 eran "la otra persona escribió y él reaccionó" -- exactamente lo que
-- molestaba -- y 3 al revés, "él escribió y le reaccionaron", donde el agente creía que
-- nadie le había respondido. Las dos direcciones cuentan como respuesta.
--
-- `respondido_con_emoji` dice si QUIEN DEBÍA CONTESTAR ya reaccionó al último mensaje.
-- Es un solo booleano y no uno por cada lado porque la pregunta siempre es la misma:
-- ¿seguimos esperando a alguien? Lo calcula `ingest/dms.py` al leer, y un mensaje nuevo
-- de cualquiera de los dos lo vuelve a poner en falso, porque entonces hay algo nuevo
-- que responder.
--
-- No hace falta ningún permiso nuevo de Slack: las reacciones ya vienen dentro de
-- `conversations.history`, que es lo que el rastro lee. Reaccionar sigue siendo
-- imposible para el agente: `reactions.add` está en la lista de lo prohibido.
--
-- 0001 a 0032 ya están aplicadas: nunca se tocan. Esto solo añade.

alter table public.dm_rastro
    add column respondido_con_emoji boolean not null default false;
