import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

// Cliente para Server Components, Server Actions y Route Handlers. Usa la
// sesión del usuario: todo lo que ve o cambia pasa por las políticas RLS de la
// base, que son las que deciden de verdad.
export async function createClient() {
  const store = await cookies();
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    {
      cookies: {
        getAll: () => store.getAll(),
        setAll: (list) => {
          try {
            list.forEach(({ name, value, options }) => store.set(name, value, options));
          } catch {
            // Desde un Server Component no se pueden escribir cookies; el proxy
            // ya renueva la sesión en cada petición.
          }
        },
      },
    },
  );
}
