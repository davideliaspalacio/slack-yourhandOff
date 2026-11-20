-- Dos arreglos: el enlace que abre el mensaje en el teléfono, y el tope diario de los
-- avisos de canal.
--
-- 1. **El enlace de la app** (`delivery/enlace_app.py`). Casi todos los avisos enlazan
--    al mensaje original del Founders Club con el enlace web de `chat.getPermalink`. En
--    el ordenador abre el navegador y lleva al mensaje; en el teléfono AG se queda en
--    una página del navegador y, al saltar a Slack, cae en la app sin llegar al mensaje.
--    Junto al enlace web va ahora un segundo enlace, "Open in app", con el esquema
--    `slack://`. Para montarlo hace falta el código del workspace del Founders Club
--    (`team_id`), que el worker lee de `auth.test` y deja en `config`.
--
-- 2. **El tope de menciones y presentaciones** (`signals/eventos.py`). El tope contaba
--    las filas `publicada` por `created_at`, que es cuándo se APUNTÓ el evento y no
--    cuándo salió el aviso. Con `presentaciones_por_dia` en 0 durante un mes se
--    acumularon 303 presentaciones `pendiente`; al subirlo a 20, cada vuelta del worker
--    publicó otras 20 de las viejas, porque ninguna contaba como "de hoy" (salieron 52
--    antes de cortarlo a mano). Dos cambios: el tope cuenta por el momento de
--    publicación (`publicado_at`), y un evento pendiente cuyo mensaje tiene más de
--    `eventos_edad_maxima_horas` no se publica nunca -- se cierra como `omitida`.
--
-- 0001 a 0040 ya están aplicadas: nunca se tocan. Esto solo añade.

-- Cuándo salió el aviso. Se escribe al reservar (antes de publicar), como en
-- `miembros_nuevos` (0037), y se borra si la publicación falla.
alter table public.eventos_canal
    add column publicado_at timestamptz;

-- Lo ya publicado: el momento real está en `aviso_ts`, que es el `ts` que devolvió
-- Slack al publicar. Sin esto, lo que salió hoy antes de aplicar la migración no
-- contaría contra el tope de hoy. Las filas sin `aviso_ts` (una caída entre reservar y
-- anotar) se quedan con cuándo se apuntaron, que es lo mejor que se sabe de ellas.
update public.eventos_canal
   set publicado_at = case
           when aviso_ts ~ '^[0-9]+(\.[0-9]+)?$' then to_timestamp(aviso_ts::numeric)
           else created_at
       end
 where estado = 'publicada';

create index eventos_canal_publicados_idx on public.eventos_canal (tipo, publicado_at)
    where estado = 'publicada';

insert into public.config (key, value) values
    -- El enlace "Open in app" de los avisos. Apagado, los avisos salen exactamente como
    -- antes de esta migración: solo con el enlace web.
    ('enlace_app_activo', 'true'::jsonb),
    -- El código del workspace del Founders Club (T…). Lo escribe el worker con lo que
    -- contesta `auth.test`; vacío, los avisos salen sin enlace de app. No es un secreto
    -- (va dentro de cualquier enlace de Slack) y no se edita desde el panel.
    ('founders_club_team_id', '""'::jsonb),
    -- Hasta qué edad del mensaje original puede salir un aviso de mención o de
    -- presentación que se quedó en `pendiente`. Pasado el plazo se cierra sin publicar:
    -- subir un tope o encender un interruptor no suelta lo atrasado.
    ('eventos_edad_maxima_horas', '24'::jsonb)
on conflict (key) do nothing;

-- Política propia, como cada grupo de claves desde 0012: booleano el interruptor,
-- entero acotado el plazo (de 1 hora a 7 días), con el mismo cast protegido de 0023.
-- `founders_club_team_id` no entra: solo lo escribe el worker.
create policy panel_ajusta_enlace_app on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('enlace_app_activo', 'eventos_edad_maxima_horas')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'enlace_app_activo' and jsonb_typeof(value) = 'boolean')
            or (key = 'eventos_edad_maxima_horas'
                and jsonb_typeof(value) = 'number'
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 1
                              and (value #>> '{}')::numeric <= 168
                              and (value #>> '{}')::numeric = trunc((value #>> '{}')::numeric)
                         else false end)
        )
    );

-- El plazo NO entra en `mcp_ajustar_umbral`, igual que las claves de 0037 y 0039:
-- añadirlo obliga a reescribir la función entera. Se cambia desde Settings.
