-- Cierres de la revisión de seguridad del panel (2026-09-22).
--
-- Nada de esto era explotable: son capas de más. 0001 a 0008 ya están en
-- producción y no se tocan.

-- valid_research_links (0008) quedó ejecutable por PUBLIC, así que PostgREST
-- la expone sin sesión. No lee nada -- devuelve un booleano sobre su propio
-- argumento -- pero no hay razón para publicarla.
-- Ojo: la restricción CHECK de prospects.research_links la llama, y una
-- restricción se evalúa con los permisos de quien escribe: sin EXECUTE para
-- authenticated, cualquier update del panel sobre esa tabla falla.
revoke all on function public.valid_research_links(text[]) from public, anon;
grant execute on function public.valid_research_links(text[]) to authenticated;

-- La vista de costes es security_invoker, así que anon ya no saca filas (no
-- tiene permiso sobre llm_calls ni cost_events). Se revoca igual, para que el
-- permiso no dependa de eso.
revoke all on public.panel_costes_diarios from anon;
