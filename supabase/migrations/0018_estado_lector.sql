-- Latido del lector de Slack: lo escribe el worker en cada sondeo para que el
-- panel enseñe si la credencial pegada funciona de verdad.
--
-- Vive en `config` (jsonb), clave `lector_estado`. El panel ya puede leerla
-- (política panel_lee_config, 0006). La escritura NO se abre al panel: la
-- política panel_ajusta_umbrales sigue limitada a su lista blanca de claves y
-- el worker escribe directo como dueño de la base.
--
-- Fila vacía = "el worker todavía no ha sondeado".
--
-- 0001 a 0017 ya están en producción: nunca se tocan. Esto solo añade.

insert into public.config (key, value)
values ('lector_estado', '{}'::jsonb)
on conflict (key) do nothing;
