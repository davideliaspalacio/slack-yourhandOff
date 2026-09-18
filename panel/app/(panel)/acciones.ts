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
