import { NextResponse, type NextRequest } from "next/server";

// Redirige dentro del mismo host por el que llegó la petición. `request.url`
// no sirve: en desarrollo Next lo reescribe a localhost aunque se entre por
// 127.0.0.1, y las cookies de la sesión se quedan en el otro host.
//
// El host se toma de las cabeceras, así que se comprueba antes de usarlo: en
// Vercel las pone el borde y no el cliente, pero detrás de otro proxy una
// cabecera inventada convertiría esto en un redirect abierto -- y el `?code=`
// de Supabase acabaría en el host del atacante.
function hostPermitido(host: string | null): string | null {
  if (!host) return null;
  const nombre = host.split(":")[0].toLowerCase();
  const propios = [
    process.env.VERCEL_PROJECT_PRODUCTION_URL,
    process.env.VERCEL_URL,
    process.env.NEXT_PUBLIC_SITE_HOST,
  ].filter(Boolean) as string[];
  const permitido =
    nombre === "localhost" ||
    nombre === "127.0.0.1" ||
    nombre.endsWith(".vercel.app") ||
    propios.some((propio) => propio.split(":")[0].toLowerCase() === nombre);
  return permitido ? host : null;
}

export function redirectTo(request: NextRequest, path: string) {
  const host =
    hostPermitido(request.headers.get("x-forwarded-host")) ??
    hostPermitido(request.headers.get("host"));
  if (!host) {
    // Host desconocido: se redirige con el origen que Next ya resolvió.
    return NextResponse.redirect(new URL(path, request.nextUrl.origin));
  }
  const proto =
    request.headers.get("x-forwarded-proto") ?? request.nextUrl.protocol.replace(":", "");
  return NextResponse.redirect(`${proto}://${host}${path}`);
}
