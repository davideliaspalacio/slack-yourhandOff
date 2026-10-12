// Adónde volver tras iniciar sesión. Solo rutas del propio panel: lo que llega
// en `?next=` lo controla quien construye el enlace, así que cualquier cosa que
// pueda salirse del host (`//otro.com`, `/\otro.com`, `https://...`) se
// descarta y se vuelve a la raíz.
export function rutaSegura(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/";
  if (/[\u0000-\u001f\u007f]/.test(next)) return "/";
  try {
    const url = new URL(next, "http://panel.invalid");
    if (url.origin !== "http://panel.invalid") return "/";
    // Volver a /login o /auth sería un bucle.
    if (url.pathname.startsWith("/login") || url.pathname.startsWith("/auth")) return "/";
    return url.pathname + url.search;
  } catch {
    return "/";
  }
}
