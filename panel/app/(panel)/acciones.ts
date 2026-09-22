"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { ESTADOS_EDITABLES } from "@/lib/format";

// Las tres acciones del panel. RLS rechaza cualquier cosa fuera de ellas; estas
// comprobaciones solo evitan viajes inútiles y dan mensajes claros.

export async function cambiarEstado(formData: FormData) {
  const id = String(formData.get("id"));
  const estado = String(formData.get("estado"));
  if (!ESTADOS_EDITABLES.includes(estado)) return;
  const supabase = await createClient();
  await supabase.from("prospects")
    .update({ state: estado, updated_at: new Date().toISOString() })
    .eq("id", id);
  revalidatePath(`/personas/${id}`);
  revalidatePath("/personas");
}

export async function volverAInvestigar(formData: FormData) {
  const id = String(formData.get("id"));
  const supabase = await createClient();
  // El slack_user_id se lee de la base por el id de la persona, no del
  // formulario: si viniera del navegador, alguien con acceso podría encolar
  // research de cualquier id inventado y gastar dinero del presupuesto.
  const { data: persona } = await supabase
    .from("prospects")
    .select("slack_user_id")
    .eq("id", id)
    .single();
  if (!persona) return;
  // La cola solo admite una tarea abierta por persona: un segundo clic choca
  // contra el índice y no encola nada.
  await supabase
    .from("research_jobs")
    .insert({ slack_user_id: persona.slack_user_id, reason: "manual" });
  revalidatePath(`/personas/${id}`);
}

export type AyudarResearchState = { error?: string };

// Lo que el equipo ya sabe y el agente no: el nombre real de la empresa
// cuando el título de Slack lo dice mal o no lo dice, la web, enlaces sueltos
// (noticias, blog, la página de equipo, hasta un perfil de LinkedIn que nunca
// se descarga pero sí se lista) y notas libres para orientar el encaje.
// panel_ayudar_research (RLS, 0008_ayuda_research.sql) normaliza y valida
// las cuatro cosas, guarda el override y -- si la persona no está descartada
// -- encola un research manual, igual que panel_corregir_web (que sigue
// existiendo tal cual, por si algo de producción todavía la llama). Todo lo
// que este action hace es partir los enlaces por línea y pasar el error de
// la función a la pantalla: la validación de verdad vive en la base.
export async function ayudarResearch(
  _previo: AyudarResearchState,
  formData: FormData,
): Promise<AyudarResearchState> {
  const id = String(formData.get("id"));
  const empresa = String(formData.get("empresa") ?? "");
  const dominio = String(formData.get("dominio") ?? "");
  const notas = String(formData.get("notas") ?? "");
  const links = String(formData.get("links") ?? "")
    .split("\n")
    .map((link) => link.trim())
    .filter(Boolean);
  const supabase = await createClient();
  const { error } = await supabase.rpc("panel_ayudar_research", {
    p_prospect: id,
    p_empresa: empresa,
    p_dominio: dominio,
    p_links: links,
    p_notas: notas,
  });
  if (error) return { error: error.message };
  revalidatePath(`/personas/${id}`);
  return {};
}

export type SimularPersonaState = { error?: string; id?: string };

// Modo de pruebas: lo mismo que scripts/simular.py mensaje, pero desde el
// panel y sin tocar Python. panel_simular_persona (RLS, 0010_modo_pruebas.sql)
// calcula el slack_user_id falso, guarda la persona y su mensaje (si lo hay,
// tal como lo dejaría el vigilante de Slack) y encola un research manual. El
// panel nunca corre el research en sí: eso lo hace el worker de Railway, que
// ya sabe drenar research_jobs.
export async function simularPersona(
  _previo: SimularPersonaState,
  formData: FormData,
): Promise<SimularPersonaState> {
  const nombre = String(formData.get("nombre") ?? "");
  const empresa = String(formData.get("empresa") ?? "");
  const web = String(formData.get("web") ?? "");
  const mensaje = String(formData.get("mensaje") ?? "");
  const notas = String(formData.get("notas") ?? "");
  const links = String(formData.get("links") ?? "")
    .split("\n")
    .map((link) => link.trim())
    .filter(Boolean);
  // Un checkbox sin marcar no manda ningún campo: su ausencia es "no publicar".
  const publicarTarjeta = formData.get("publicar_tarjeta") != null;
  const supabase = await createClient();
  const { data, error } = await supabase.rpc("panel_simular_persona", {
    p_nombre: nombre,
    p_empresa: empresa,
    p_web: web,
    p_mensaje: mensaje,
    p_links: links,
    p_notas: notas,
    p_sin_tarjeta: !publicarTarjeta,
  });
  if (error) return { error: error.message };
  revalidatePath("/pruebas");
  return { id: data as string };
}

export type BorrarSimuladosResult = { borrados: number } | { error: string };

// Borra solo lo que este modo (o scripts/simular.py, o tarjeta_prueba.py) creó
// -- panel_borrar_simulados nunca toca a nadie más. No es un <form action>
// normal porque el botón necesita el número de personas borradas para
// mostrarlo, así que un componente cliente la llama directamente.
export async function borrarSimulados(): Promise<BorrarSimuladosResult> {
  const supabase = await createClient();
  const { data, error } = await supabase.rpc("panel_borrar_simulados");
  if (error) return { error: error.message };
  revalidatePath("/pruebas");
  return { borrados: data as number };
}

export async function refrescarPruebas() {
  revalidatePath("/pruebas");
}

export async function guardarUmbral(formData: FormData) {
  const key = String(formData.get("key"));
  const value = Number(formData.get("value"));
  if (!Number.isInteger(value) || value < 0) return;
  const supabase = await createClient();
  await supabase.from("config")
    .update({ value, updated_at: new Date().toISOString() })
    .eq("key", key);
  revalidatePath("/ajustes");
}

export async function salir() {
  const supabase = await createClient();
  await supabase.auth.signOut();
  redirect("/login");
}
