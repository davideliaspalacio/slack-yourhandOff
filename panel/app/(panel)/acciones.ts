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
  const slackUserId = String(formData.get("slack_user_id"));
  const supabase = await createClient();
  // La cola solo admite una tarea abierta por persona: un segundo clic choca
  // contra el índice y no encola nada.
  await supabase.from("research_jobs").insert({ slack_user_id: slackUserId, reason: "manual" });
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
