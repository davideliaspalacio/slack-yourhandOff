import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";
import { redirectTo } from "./lib/redirect";

// Renueva la sesión en cada petición y manda al login a quien no la tiene.
// Es una comprobación rápida, no la seguridad: quien decide qué se ve es RLS.
export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });
  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    {
      cookies: {
        getAll: () => request.cookies.getAll(),
        setAll: (list) => {
          list.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          list.forEach(({ name, value, options }) => response.cookies.set(name, value, options));
        },
      },
    },
  );

  const path = request.nextUrl.pathname;
  // Si Supabase no tiene autorizada la ruta de vuelta, manda el enlace del correo
  // a la raíz con ?code=. Se reencamina al callback para no perder el acceso.
  const code = request.nextUrl.searchParams.get("code");
  if (code && !path.startsWith("/auth/callback")) {
    return redirectTo(request, `/auth/callback?code=${encodeURIComponent(code)}`);
  }

  const { data } = await supabase.auth.getUser();
  const isPublic = path.startsWith("/login") || path.startsWith("/auth");
  if (!data.user && !isPublic) {
    return redirectTo(request, "/login");
  }
  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
