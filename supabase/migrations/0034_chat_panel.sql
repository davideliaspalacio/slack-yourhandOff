-- El chat del Founders Club dentro del panel, y poder responder desde ahí.
--
-- Hasta aquí el agente NO PODÍA escribir en el Founders Club, y eso estaba sostenido por
-- cuatro capas: la lista blanca de métodos del lector, la lista negra de lo prohibido, un
-- test que intenta escribir y comprueba que falla, y que quien publica es otro bot en
-- otro workspace. Esta migración abre esa puerta a propósito, porque el equipo necesita
-- poder contestar cuando AG no está. Anthony lo autorizó (comunicado por David el
-- 2026-11-09), solo para David Rojas.
--
-- Lo que NO cambia, y es importante: el lector sigue sin poder escribir. `READ_METHODS`
-- y la lista negra se quedan como están, con su test. Lo que envía es una pieza aparte
-- (`tools/respondedor.py`), que solo sabe mandar un mensaje y nada más: ni reaccionar, ni
-- editar, ni borrar, ni unirse a un canal.
--
-- Los límites que contienen esto, y por qué cada uno:
--
-- - **Nunca se manda solo.** No hay ni un camino automático: una persona escribe el texto
--   y pulsa enviar. Sin IA, por petición expresa.
-- - **Solo quien tenga el permiso** (`panel_users.puede_responder`), que hoy es una sola
--   persona. El resto del equipo ve el chat y no puede escribir.
-- - **Solo donde ya hay conversación**: canales que ya vigilamos y DMs que ya existen.
--   No se puede abrir una conversación nueva ni escribir en un canal cualquiera del club.
-- - **Todo queda registrado** en `mensajes_enviados`: quién lo mandó desde el panel, a
--   dónde, cuándo y qué. Los mensajes salen como Anthony, así que tiene que poder
--   saberse quién los escribió de verdad.
-- - **Tope diario** e interruptor, apagado al nacer.
--
-- 0001 a 0033 ya están aplicadas: nunca se tocan. Esto solo añade.

-- Quién puede enviar. Por omisión nadie: ver el chat no es poder escribir en él.
alter table public.panel_users add column puede_responder boolean not null default false;

insert into public.config (key, value) values
    -- El interruptor de enviar. Apagado, el panel es solo de lectura.
    ('chat_responder_activo', 'false'::jsonb),
    -- Tope de mensajes al día entre todos, para que un error no se vuelva una ráfaga.
    ('chat_mensajes_por_dia', '50'::jsonb)
on conflict (key) do nothing;

create policy panel_ajusta_chat on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('chat_responder_activo', 'chat_mensajes_por_dia')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'chat_responder_activo' and jsonb_typeof(value) = 'boolean')
            or (key = 'chat_mensajes_por_dia'
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023).
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0 else false end)
        )
    );

-- El registro de lo enviado. Se escribe SIEMPRE, incluso si Slack falla después: primero
-- se deja constancia de la intención y luego se manda, como el resumen diario, para que
-- no pueda existir un mensaje enviado del que no haya rastro.
create table public.mensajes_enviados (
    id           uuid primary key default gen_random_uuid(),
    -- A dónde fue: un canal (C…/G…) o una conversación directa (D…).
    destino      text not null check (destino ~ '^[CGD][A-Z0-9]+$'),
    -- El hilo, cuando se responde dentro de uno.
    thread_ts    text,
    -- Quién lo escribió DE VERDAD, aunque en Slack aparezca como Anthony.
    autor_email  text not null,
    texto        text not null,
    -- Lo que Slack devolvió al publicarlo, para poder señalar el mensaje después.
    enviado_ts   text,
    estado       text not null default 'pendiente'
                 check (estado in ('pendiente', 'enviado', 'fallido')),
    motivo       text,
    created_at   timestamptz not null default now()
);

create index mensajes_enviados_por_fecha_idx on public.mensajes_enviados (created_at desc);

alter table public.mensajes_enviados enable row level security;
-- Lo lee el panel entero: que todo el equipo pueda ver quién escribió qué es justamente
-- el punto. Escribir es otra cosa y pasa por el worker, no por la API.
grant select on public.mensajes_enviados to authenticated;
create policy panel_lee_enviados on public.mensajes_enviados for select to authenticated
    using (public.is_panel_user());
