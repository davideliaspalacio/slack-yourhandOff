-- Llevar los leads al CRM: empresa, contacto y una nota con el research.
--
-- Hasta aquí el agente encontraba el lead, se lo contaba a Anthony por Slack y ahí se
-- acababa: no quedaba rastro en HubSpot, el equipo comercial no lo veía, y nadie podía
-- decir si una tarjeta acabó en negocio.
--
-- Cuándo se escribe, y por qué ahí: cuando una persona pasa a `contactado` o `cliente`.
-- Una tarjeta es "mira esto"; un registro en el CRM significa que alguien se comprometió
-- con esa relación. Si entrara cada tarjeta, el CRM se llenaría de gente que nadie
-- eligió y los informes del equipo dejarían de significar nada.
--
-- Qué se necesitaba saber y ya está medido contra su cuenta real (portal 245974710):
--
-- - La llave permite empresas, contactos, notas y crear campos. Las notas eran la duda,
--   porque la documentación de HubSpot tiene esa sección vacía; se comprobó llamando.
-- - Hay sitio de sobra para campos propios: usan 9 de 1.000 en empresas y 10 de 1.000 en
--   contactos. No hace falta meter el research dentro de la nota por falta de espacio.
-- - El 100% de los perfiles del club trae correo (60 de 60 en la muestra): 52 de empresa
--   y 8 personales. Por eso sí se pueden crear contactos de verdad, que es lo que hace
--   útil la integración -- el correo es la única llave que evita duplicarlos.
--
-- `prospects.email` nace de ahí. Es un dato personal de gente de una comunidad privada,
-- así que: solo el del perfil público del club, solo para quien llegue a `contactado`, y
-- el panel no lo enseña salvo en la ficha de la persona.
--
-- 0001 a 0034 ya están aplicadas: nunca se tocan. Esto solo añade.

-- El correo del perfil de Slack. Hasta ahora se leía para deducir el dominio de la
-- empresa (`ingest/profile_hints.domain_from_email`) y se tiraba.
alter table public.prospects add column email text;

-- El estado de la sincronización, con la misma forma que `alerta_*` (0022): reservar
-- antes de llamar, contar intentos, y un motivo legible cuando no se hace.
alter table public.prospects
    add column hubspot_contact_id   text,
    add column hubspot_company_id   text,
    add column hubspot_estado       text
        check (hubspot_estado is null
               or hubspot_estado in ('reservada', 'sincronizada', 'omitida')),
    add column hubspot_motivo       text,
    add column hubspot_at           timestamptz,
    add column hubspot_intentos     integer not null default 0;

-- Dos ids distintos no pueden apuntar a la misma persona: es la última red contra los
-- duplicados, por debajo de la búsqueda por correo y por dominio.
create unique index prospects_hubspot_contact_idx on public.prospects (hubspot_contact_id)
    where hubspot_contact_id is not null;
create index prospects_hubspot_pendientes_idx on public.prospects (updated_at)
    where hubspot_estado is null;

insert into public.config (key, value) values
    -- Apagado al nacer, como todo.
    ('hubspot_activo', 'false'::jsonb),
    -- Cuántas personas se sincronizan al día. Es el freno contra una ráfaga.
    ('hubspot_por_dia', '25'::jsonb),
    -- Nada anterior a esta marca se sincroniza: el histórico no entra solo. Lo escribe
    -- el worker la primera vez que arranca con esto. Ya nos pasó una vez que una
    -- funcionalidad nueva procesara meses de golpe.
    ('hubspot_desde', '""'::jsonb)
on conflict (key) do nothing;

create policy panel_ajusta_hubspot on public.config for update to authenticated
    using (
        public.is_panel_user()
        and key in ('hubspot_activo', 'hubspot_por_dia')
    )
    with check (
        public.is_panel_user()
        and (
            (key = 'hubspot_activo' and jsonb_typeof(value) = 'boolean')
            or (key = 'hubspot_por_dia'
                and jsonb_typeof(value) = 'number'
                -- El cast solo se evalúa si el tipo es número (ver 0023).
                and case when jsonb_typeof(value) = 'number'
                         then (value #>> '{}')::numeric >= 0 else false end)
        )
    );

-- La llave de HubSpot en la tabla de secretos, que el panel escribe y nunca lee. Mismo
-- contrato que el token de Slack (0014): guardar, saber si hay una, y borrarla.
create function public.panel_guardar_token_hubspot(p_token text) returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    if p_token is null or btrim(p_token) = '' then
        raise exception 'empty token';
    end if;
    -- El valor recortado con "…" es el error más común al pegar desde una pantalla.
    if p_token like '%…%' or p_token like '%...%' then
        raise exception 'the value looks truncated';
    end if;
    insert into public.secretos (key, value, updated_at)
    values ('hubspot_token', btrim(p_token), now())
    on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at;
end;
$$;

create function public.panel_estado_token_hubspot()
    returns table (configurado boolean, ultimos4 text, actualizado timestamptz)
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    -- Nunca el valor: solo si hay uno y sus cuatro últimos, para reconocerlo.
    return query
        select true, right(s.value, 4), s.updated_at
          from public.secretos s where s.key = 'hubspot_token' and s.value <> '';
end;
$$;

create function public.panel_borrar_token_hubspot() returns void
    language plpgsql
    security definer
    set search_path = ''
as $$
begin
    if not public.is_panel_user() then
        raise exception 'not authorized';
    end if;
    delete from public.secretos where key = 'hubspot_token';
end;
$$;

revoke all on function public.panel_guardar_token_hubspot(text) from public, anon;
revoke all on function public.panel_estado_token_hubspot() from public, anon;
revoke all on function public.panel_borrar_token_hubspot() from public, anon;
