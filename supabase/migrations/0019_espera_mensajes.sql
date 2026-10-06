-- Dejar que los mensajes de una persona se asienten antes de investigarla.
--
-- La gente suele escribir varios mensajes seguidos. Un research disparado por
-- un mensaje no se puede reclamar hasta `ts del último mensaje + este valor`
-- (segundos), de modo que el research lea el hilo entero y no solo el primero.
--
-- Es un suelo, no un retraso añadido: el lector de Slack ya sondea cada ~2
-- minutos, así que con 60 s casi nunca se espera de más. Solo afecta a los
-- jobs de motivo 'mensaje'; miembro_nuevo, manual y radar se reclaman al
-- instante, igual que antes. `research_jobs.not_before` ya existe (0004).
--
-- 0001 a 0018 ya están aplicadas: nunca se tocan. Esto solo añade.

insert into public.config (key, value)
values ('espera_mensajes_segundos', '60'::jsonb)
on conflict (key) do nothing;
