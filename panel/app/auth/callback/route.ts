import { NextResponse, type NextRequest } from "next/server";
import { createClient } from "@/lib/supabase/server";

// Aquí aterriza el enlace del correo: se canjea el código por una sesión.
export async function GET(request: NextRequest) {
  const code = request.nextUrl.searchParams.get("code");
  if (code) {
    const supabase = await createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) return NextResponse.redirect(new URL("/personas", request.url));
  }
  return NextResponse.redirect(new URL("/login?error=1", request.url));
}
