import { NextResponse, type NextRequest } from "next/server";

// Redirige dentro del mismo host por el que llegó la petición. `request.url`
// no sirve: en desarrollo Next lo reescribe a localhost aunque se entre por
// 127.0.0.1, y las cookies de la sesión se quedan en el otro host.
export function redirectTo(request: NextRequest, path: string) {
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  const proto =
    request.headers.get("x-forwarded-proto") ?? request.nextUrl.protocol.replace(":", "");
  return NextResponse.redirect(`${proto}://${host}${path}`);
}
