-- Decisor del radar (spec 2026-09-22, §6): el motivo 'radar' en la cola.
--
-- El decisor elegido para una vacante entra en prospects como
-- 'li:<linkedin_id>' y se investiga por la misma cola research_jobs que
-- cualquier otra persona. La cola guarda por qué se investiga a alguien, y
-- el CHECK de 0004 solo admitía los motivos de Slack y del panel.
--
-- 0001 a 0012 ya están en producción: nunca se tocan. Esto solo añade.

alter table research_jobs drop constraint research_jobs_reason_check;
alter table research_jobs add constraint research_jobs_reason_check
    check (reason in ('mensaje', 'miembro_nuevo', 'manual', 'radar'));
