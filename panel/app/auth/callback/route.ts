import type { NextRequest } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { redirectTo } from "@/lib/redirect";

// Aquí aterriza el enlace del correo: se canjea el código por una sesión.
export async function GET(request: NextRequest) {
  const code = request.nextUrl.searchParams.get("code");
  if (!code) return redirectTo(request, "/login?error=sin-codigo");

  const supabase = await createClient();
  const { error } = await supabase.auth.exchangeCodeForSession(code);
  if (error) {
    console.error("auth/callback: could not exchange the code:", error.message);
    return redirectTo(request, "/login?error=enlace");
  }
  return redirectTo(request, "/personas");
}
