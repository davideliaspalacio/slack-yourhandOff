import { cache } from "react";
import { createClient } from "@/lib/supabase/server";

// Quién mira y si la base le deja ver datos. `cache` lo comparte entre el layout
// y la página de la misma petición: una sola consulta a la sesión y a
// is_panel_user() en vez de una por componente.
//
// Hace falta porque RLS contesta "vacío" igual cuando la fila no existe que
// cuando quien mira no está en panel_users. Una página que va a llamar a
// notFound() tiene que preguntar esto antes: sin permiso no es un 404.
export const acceso = cache(async () => {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { supabase, user: null, allowed: false };
  const { data: allowed } = await supabase.rpc("is_panel_user");
  return { supabase, user, allowed: allowed === true };
});
