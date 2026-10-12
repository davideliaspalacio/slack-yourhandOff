-- Alertas de señales en Slack: qué se publicó, cuándo y dónde.
--
-- El registro de cada alerta vive en la propia fila de message_signals y no en
-- `deliveries` (cuyo kind admite slack/sms/email) por dos razones:
--   1. deliveries.prospect_id y deliveries.band son NOT NULL, y una señal puede
--      venir de alguien que todavía no es un prospect (message_signals.prospect_id
--      es nulo en ese caso). Registrarla ahí obligaría a inventar persona y banda.
--   2. "¿esta señal ya se publicó?" es una pregunta sobre la señal: con las
--      columnas aquí, la respuesta es mirar su propia fila (alerta_estado), sin
--      cruzar tablas ni depender de una clave de unión.
-- Las tarjetas de research siguen en deliveries, intactas.
--
-- alerta_estado:
--   null        pendiente: aún no se ha decidido nada (o se liberó tras un fallo)
--   reservada   alguien la está publicando (impide publicar dos veces)
--   publicada   salió a Slack: alerta_channel_id y alerta_ts dicen dónde
--   omitida     decisión final de no publicarla: alerta_motivo dice por qué
--
-- 0001 a 0021 ya están en el repositorio: nunca se tocan. Esto solo añade.

alter table public.message_signals
    add column alerta_estado text
        check (alerta_estado is null or alerta_estado in ('reservada', 'publicada', 'omitida')),
    add column alerta_motivo text,
    add column alerta_channel_id text,
    add column alerta_ts text,
    add column alerta_at timestamptz,
    add column alerta_intentos integer not null default 0,
    add constraint message_signals_alerta_publicada_completa
        check (alerta_estado is distinct from 'publicada'
               or (alerta_channel_id is not null and alerta_ts is not null)),
    add constraint message_signals_alerta_omitida_con_motivo
        check (alerta_estado is distinct from 'omitida' or alerta_motivo is not null);

-- Para el tope diario y el de "una por persona y periodo".
create index message_signals_alerta_idx
    on public.message_signals (alerta_at desc)
    where alerta_estado in ('reservada', 'publicada');

insert into public.config (key, value) values
    -- Mensajes más viejos que esto no generan alerta (evita inundar el canal con
    -- el histórico en el primer arranque).
    ('alertas_edad_maxima_horas', '24'::jsonb),
    -- Como mucho un aviso de credencial caducada del lector cada tantas horas.
    ('aviso_token_horas', '6'::jsonb)
on conflict (key) do nothing;

-- Editables desde Settings: se amplía la lista blanca de 0021 con las dos claves
-- nuevas (una política se reemplaza entera, no se parcha). Mismo molde: solo
-- números, solo para quien está en el panel.
drop policy panel_ajusta_senales on public.config;
create policy panel_ajusta_senales on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('senales_activas', 'alertas_por_dia', 'alertas_dias_misma_persona',
                    'alertas_edad_maxima_horas', 'aviso_token_horas')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'senales_activas' and jsonb_typeof(value) = 'boolean')
            or (key in ('alertas_por_dia', 'alertas_dias_misma_persona',
                        'alertas_edad_maxima_horas', 'aviso_token_horas')
                and jsonb_typeof(value) = 'number')
        )
    );
