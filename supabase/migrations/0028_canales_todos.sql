-- Vigilar todos los canales en los que está la cuenta lectora, sin listarlos a mano.
--
-- Hasta aquí los canales vigilados eran una lista escrita a mano (`slack_channel_ids`,
-- 0016) con un tope de 20. Son 4, y la medición dijo que la cuenta está dentro de 34:
-- los 4 vigilados traen 669 mensajes de personas en 30 días y los 34 traen 1.196. Lo
-- que se estaba perdiendo no es poco y además está en los sitios obvios -- hay un
-- `0-job-board` sin vigilar.
--
-- Esta migración añade el modo automático. Dos cosas:
--
-- 1. `slack_canales_todos`: un interruptor, APAGADO por defecto. Apagado, todo sigue
--    exactamente como hoy -- la lista a mano y su tope de 20, que no se tocan, porque
--    el tope gobierna esa lista y ahí sigue teniendo sentido. Encendido, los canales
--    salen de esta tabla: los que el descubrimiento vio con la cuenta dentro.
--
-- 2. Las columnas que hacen falta para no desperdiciar llamadas. Sondear 34 canales
--    cada 2 minutos serían ~70 llamadas por vuelta (historial, miembros y a veces el
--    nombre), y la lista de miembros de un canal de 1.400 personas son siete páginas.
--    Con la cola larga del club casi muerta -- hay canales con 0 mensajes en un mes --
--    eso es tirar llamadas. Así que cada canal guarda cuándo le toca otra vez:
--    `ingest/watcher.py` lo acerca cuando el canal trae mensajes y lo aleja cuando no.
--
-- Nada de esto puede escribir en Slack: descubrir canales es `conversations.list`, que
-- ya estaba en la lista blanca de solo lectura del lector (se usa para los DMs). El
-- agente nunca se une a un canal -- unirse sí se vería en el canal -- así que un canal
-- donde la cuenta no está se guarda igual, para poder enseñarlo, pero no se sondea.
--
-- 0001 a 0027 ya están aplicadas: nunca se tocan. Esto solo añade.

insert into public.config (key, value)
values ('slack_canales_todos', 'false'::jsonb)
on conflict (key) do nothing;

-- Editable desde Settings, como exigir_evidencia (0020) y seguimiento_activo (0027):
-- las políticas permisivas se combinan con OR, así que la lista blanca solo crece en
-- esta clave. Es un interruptor, no un umbral: el with check no deja guardar nada que
-- no sea true o false.
create policy panel_ajusta_canales_todos on public.config for update to authenticated
    using (public.is_panel_user() and key = 'slack_canales_todos')
    with check (
        public.is_panel_user()
        and key = 'slack_canales_todos'
        and jsonb_typeof(value) = 'boolean'
    );

-- ---------------------------------------------------------------------------------
-- Lo que el descubrimiento averigua de cada canal, y lo que la rotación necesita.
-- Todas con valor por omisión o nulas: las filas que ya existen siguen valiendo, y el
-- modo a mano no lee ninguna de estas columnas.
-- ---------------------------------------------------------------------------------
alter table public.canales
    -- Si la cuenta lectora está dentro. Null = todavía no se ha descubierto nada de
    -- este canal (las filas de 0025 las escribió el watcher al pedir el nombre).
    add column es_miembro            boolean,
    -- Cuándo lo vio el descubrimiento por última vez. Un canal que desaparece de la
    -- lista (lo archivaron, o la cuenta salió) deja de sondearse por `es_miembro`.
    add column visto_at              timestamptz,
    -- Cuándo se leyó su historial por última vez, y cuándo le toca otra vez.
    add column sondeado_at           timestamptz,
    add column siguiente_sondeo_at   timestamptz,
    -- Vueltas seguidas sin un mensaje nuevo: es la escalera del retroceso. Se pone a
    -- cero en cuanto el canal trae algo.
    add column vueltas_vacias        integer not null default 0,
    -- Mensajes de personas que ha traído en total. No gobierna nada; es para que el
    -- panel y el MCP puedan decir qué canales valen la pena.
    add column mensajes_vistos       integer not null default 0,
    -- La lista de miembros va a su propio ritmo, mucho más lento: un miembro nuevo no
    -- necesita enterarse en 2 minutos y es la llamada más cara que hay.
    add column miembros_revisados_at timestamptz;

-- La consulta de la rotación: los canales con la cuenta dentro a los que ya les toca.
-- Las filas nulas en `siguiente_sondeo_at` son canales recién descubiertos y van
-- primero, así que entran en el índice.
create index canales_por_sondear_idx on public.canales (siguiente_sondeo_at nulls first)
    where es_miembro;
