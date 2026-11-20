-- Saludar a quien se presenta y a quien entra: el saludo sugerido y el aviso de
-- miembro nuevo.
--
-- Hasta aquí el aviso de presentación (0030) decía que alguien se había presentado y
-- nada más, y de un miembro nuevo no se avisaba: solo se encolaba su research. Lo que
-- pidió el cliente es que cada uno de esos momentos le recuerde a Anthony qué hacer
-- (saludar, conectar en LinkedIn) y le dé el texto listo para copiar.
--
-- Tres cosas:
--
-- 1. **El saludo sugerido** (`eventos_canal.saludo`). Lo escribe el modelo a partir de
--    lo que la persona dijo al presentarse. Se guarda junto al evento para no pagarlo
--    dos veces si la publicación falla y se reintenta. Es una sugerencia para copiar:
--    el agente sigue sin escribir una palabra en el Founders Club.
-- 2. **`miembros_nuevos`**: una fila por persona que aparece en el padrón de un canal
--    vigilado. La escribe el watcher al detectarla y de ahí sale su aviso. Una fila por
--    persona y no por (persona, canal): quien entra en tres canales a la vez es un
--    miembro nuevo, no tres.
-- 3. Las claves de `config` de las dos cosas, y la política que deja tocarlas desde
--    Settings.
--
-- 0001 a 0036 ya están aplicadas: nunca se tocan. Esto solo añade.

alter table public.eventos_canal
    -- Solo en las presentaciones, y solo si el modelo dio uno que pasó la validación
    -- (`delivery/saludo.py`). Nulo no significa "pendiente": significa que el aviso
    -- salió, o saldrá, sin sugerencia.
    add column saludo text;

create table public.miembros_nuevos (
    id            uuid primary key default gen_random_uuid(),
    -- Una fila por persona, para siempre: es lo que impide avisar dos veces de la misma.
    slack_user_id text not null unique,
    -- El canal donde se la vio entrar primero. Solo para decirlo en el aviso.
    channel_id    text not null,
    estado        text not null default 'pendiente'
                  check (estado in ('pendiente', 'publicada', 'omitida')),
    motivo        text,
    aviso_ts      text,
    created_at    timestamptz not null default now(),
    -- Cuándo salió el aviso. El tope diario cuenta por esta fecha y no por
    -- `created_at`: alguien detectado ayer a última hora y avisado hoy gasta el cupo
    -- de hoy.
    publicado_at  timestamptz
);

create index miembros_nuevos_pendientes_idx on public.miembros_nuevos (created_at)
    where estado = 'pendiente';

alter table public.miembros_nuevos enable row level security;

-- La lee el panel y nadie más, como eventos_canal (0030).
grant select on public.miembros_nuevos to authenticated;
create policy panel_lee_miembros_nuevos on public.miembros_nuevos for select to authenticated
    using (public.is_panel_user());

insert into public.config (key, value) values
    -- El saludo sugerido cuesta una llamada corta al modelo por presentación, y las
    -- presentaciones ya tienen su tope (`presentaciones_por_dia`, 20). Nace encendido:
    -- es lo que se pidió, y apagarlo deja el aviso tal cual, sin la sugerencia.
    ('saludo_sugerido_activo', 'true'::jsonb),
    -- Cuántos avisos de miembro nuevo salen al día. 0 = ninguno.
    ('miembros_nuevos_por_dia', '20'::jsonb),
    -- Cuánto se espera a que termine el research de la persona antes de avisar sin él.
    -- Con el research hecho el aviso lleva su cargo, su empresa y su LinkedIn; sin él,
    -- solo el nombre y un enlace de búsqueda. 0 = no esperar.
    ('miembro_nuevo_espera_min', '30'::jsonb)
on conflict (key) do nothing;

create policy panel_ajusta_saludos on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('saludo_sugerido_activo', 'miembros_nuevos_por_dia',
                    'miembro_nuevo_espera_min')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'saludo_sugerido_activo' and jsonb_typeof(value) = 'boolean')
            or (key in ('miembros_nuevos_por_dia', 'miembro_nuevo_espera_min')
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023).
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0 else false end)
        )
    );

-- Los dos topes nuevos NO entran en `mcp_ajustar_umbral`. Añadirlos obliga a reescribir
-- la función entera, y la migración siguiente (0038, de otra rama) puede reescribirla
-- también: la que se aplicara última borraría las claves de la otra. Se cambian desde
-- Settings; si hace falta tocarlos desde Claude, se añaden en una migración posterior a
-- las dos.
