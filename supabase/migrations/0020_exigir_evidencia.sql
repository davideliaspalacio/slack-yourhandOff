-- Una nota de encaje sin pruebas no interrumpe a nadie.
--
-- El agente publicó una tarjeta con encaje 2/3 cuya razón decía "likely has
-- needs in support, operations, or back office, though there is no direct
-- evidence of current open roles": una suposición por sector. Desde ahora la
-- síntesis declara de dónde sale la nota (encaje_handoff.evidencia) y, con
-- esta clave activada, delivery/bands.py deja en banda baja -- panel y email
-- diario, sin tarjeta ni SMS -- todo lo que traiga evidencia "ninguna", sea
-- cual sea su puntuación.
--
-- Activada por defecto. Apagarla devuelve el comportamiento anterior: la
-- banda sale solo de la puntuación.
--
-- 0001 a 0019 ya están aplicadas: nunca se tocan. Esto solo añade.

insert into public.config (key, value)
values ('exigir_evidencia', 'true'::jsonb)
on conflict (key) do nothing;

-- Editable desde Settings. Política nueva, que se suma a panel_ajusta_umbrales
-- (0006) y panel_ajusta_radar (0012): las permisivas se combinan con OR, así
-- que la lista blanca solo crece en esta clave. Es un interruptor, no un
-- umbral: el with check no deja guardar nada que no sea true o false.
create policy panel_ajusta_evidencia on public.config for update to authenticated
    using (public.is_panel_user() and key = 'exigir_evidencia')
    with check (
        public.is_panel_user()
        and key = 'exigir_evidencia'
        and jsonb_typeof(value) = 'boolean'
    );
