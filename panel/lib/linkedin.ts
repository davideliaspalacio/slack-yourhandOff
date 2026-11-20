// El enlace de LinkedIn de una persona, para su ficha. Las mismas dos reglas que usa el
// worker en los avisos de Slack (src/handoff_agent/delivery/linkedin.py); si cambian
// allí, cambian aquí.
//
// - El perfil, cuando se conoce: un enlace que el equipo puso a mano en "Help the
//   research" o la fuente que el research citó para el cargo. Solo cuenta una URL
//   https://…linkedin.com/in/…: una página de empresa o una búsqueda no son el perfil
//   de nadie. Lo que viene del dossier lo escribió un modelo leyendo páginas ajenas, así
//   que se valida entero antes de ponerlo en un href.
// - Una búsqueda de personas, cuando no: se monta aquí con el nombre y la empresa, y la
//   ficha dice que es una búsqueda y no un perfil comprobado.

const MAX_URL = 300;
const MAX_TERMINOS = 120;

export function esPerfilLinkedin(url: unknown): url is string {
  if (typeof url !== "string" || !url || url.length > MAX_URL || /[\s|<>]/.test(url)) return false;
  let partes: URL;
  try {
    partes = new URL(url);
  } catch {
    return false;
  }
  if (partes.protocol !== "https:" || partes.username || partes.password || partes.port) return false;
  const host = partes.hostname.toLowerCase();
  if (host !== "linkedin.com" && !host.endsWith(".linkedin.com")) return false;
  return partes.pathname.startsWith("/in/") && partes.pathname.length > "/in/".length;
}

// Primero lo que el equipo puso a mano: alguien lo eligió a propósito.
export function perfilLinkedin(
  fuente: unknown, enlacesEquipo: readonly unknown[] | null | undefined,
): string | null {
  for (const enlace of enlacesEquipo ?? []) {
    if (esPerfilLinkedin(enlace)) return enlace;
  }
  return esPerfilLinkedin(fuente) ? fuente : null;
}

// Sin nombre no hay búsqueda: buscar solo por empresa no encuentra a la persona.
export function busquedaLinkedin(
  nombre: string | null | undefined, empresa?: string | null,
): string | null {
  const limpio = (nombre ?? "").trim().split(/\s+/).filter(Boolean).join(" ");
  if (!limpio) return null;
  const terminos = `${limpio} ${(empresa ?? "").trim()}`.trim().split(/\s+/).join(" ");
  return "https://www.linkedin.com/search/results/people/?keywords=" +
    encodeURIComponent(terminos.slice(0, MAX_TERMINOS));
}
