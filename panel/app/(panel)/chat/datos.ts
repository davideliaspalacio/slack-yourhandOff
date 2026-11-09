import type { createClient } from "@/lib/supabase/server";

// Todas las consultas de la vista de chat, en un sitio. La página solo pinta.
//
// Es de SOLO LECTURA, y lo es a propósito: lo que se lee aquí son conversaciones del
// Founders Club, un Slack que no es nuestro, con la sesión de AG. No hay ni una
// escritura en este fichero ni en los componentes que lo usan.

export type Supabase = Awaited<ReturnType<typeof createClient>>;

// ---------------------------------------------------------------------------------
// Tipos
// ---------------------------------------------------------------------------------

export type Autor = {
  slackUserId: string;
  nombre: string | null;
  empresa: string | null;
  prospectId: string | null;
};

export type Mensaje = {
  ts: string;
  userId: string | null;
  texto: string | null;
  respuestas: Mensaje[];
};

export type Canal = {
  kind: "canal";
  id: string;
  nombre: string | null;
  mensajesVistos: number;
};

export type Dm = {
  kind: "dm";
  id: string;
  slackUserId: string;
  prospectId: string | null;
  nombre: string | null;
  empresa: string | null;
  teamId: string | null;
  ultimoTs: string;
  ultimoDe: string | null;
  sinLeer: number | null;
};

export type Conversacion = Canal | Dm;

// Los mismos formatos que exigen la base y Slack: `canales`/`slack_channel_ids` usan
// C… o G… (0016) y `dm_rastro.dm_id` exige D… (0027). Sirve para saber, con solo mirar
// el `?c=` de la URL, si lo que se pide es un canal público o un DM privado.
export const esCanalId = (id: string) => /^[CG][A-Z0-9]{5,}$/.test(id);
export const esDmId = (id: string) => /^D[A-Z0-9]+$/.test(id);

// ---------------------------------------------------------------------------------
// Interruptores que la vista necesita conocer
// ---------------------------------------------------------------------------------

export type Interruptores = {
  // `dm_texto_activo` (0031). Apagado en producción: sin él no hay texto de DMs.
  dmTexto: boolean;
  // `leer_hilos` (0032). Apagado, las respuestas de los hilos no se traen, así que
  // un hilo puede tener respuestas en Slack y ninguna aquí.
  leerHilos: boolean;
  // `slack_canales_todos` (0028): de dónde sale la lista de canales vigilados.
  canalesTodos: boolean;
  canalesManuales: string[];
};

export async function leerInterruptores(supabase: Supabase): Promise<Interruptores> {
  const { data } = await supabase.from("config").select("key, value")
    .in("key", ["dm_texto_activo", "leer_hilos", "slack_canales_todos", "slack_channel_ids"]);
  const valores = new Map((data ?? []).map((r) => [r.key as string, r.value]));
  const manuales = valores.get("slack_channel_ids");
  return {
    // Los tres valores por omisión son los del worker: apagado salvo que la base
    // diga true (ingest/dm_texto.py, ingest/watcher.py, canales.modo_todos()).
    dmTexto: valores.get("dm_texto_activo") === true,
    leerHilos: valores.get("leer_hilos") === true,
    canalesTodos: valores.get("slack_canales_todos") === true,
    canalesManuales: Array.isArray(manuales)
      ? manuales.filter((c): c is string => typeof c === "string")
      : [],
  };
}

// ---------------------------------------------------------------------------------
// La barra lateral: canales vigilados y conversaciones directas
// ---------------------------------------------------------------------------------

/** Los canales que el agente vigila, igual que los calcula el worker.
 *
 * Con `slack_canales_todos` encendido son los que el descubrimiento vio con la cuenta
 * lectora dentro (`canales.es_miembro`); apagado, la lista escrita a mano en
 * `slack_channel_ids`. Un canal de la lista a mano puede no tener fila en `canales`
 * todavía (el worker la escribe al preguntar el nombre): entonces solo hay código. */
export async function listarCanales(
  supabase: Supabase,
  interruptores: Interruptores,
): Promise<Canal[]> {
  const { data } = await supabase.from("canales")
    .select("channel_id, nombre, es_miembro, mensajes_vistos");
  const filas = data ?? [];
  const porId = new Map(filas.map((c) => [c.channel_id as string, c]));

  const comoCanal = (id: string, fila?: { nombre?: unknown; mensajes_vistos?: unknown }): Canal => ({
    kind: "canal",
    id,
    nombre: (fila?.nombre as string | null) ?? null,
    mensajesVistos: Number(fila?.mensajes_vistos ?? 0),
  });

  if (interruptores.canalesTodos) {
    return filas
      .filter((c) => c.es_miembro === true)
      .map((c) => comoCanal(c.channel_id as string, c))
      // Los más activos primero: es el orden que ya usa Settings para esta misma lista.
      .sort((a, b) => b.mensajesVistos - a.mensajesVistos || a.id.localeCompare(b.id));
  }
  // En modo manual se respeta el orden en que están escritos.
  return interruptores.canalesManuales.map((id) => comoCanal(id, porId.get(id)));
}

type FilaRastro = {
  dm_id: string; team_id: string | null; slack_user_id: string; prospect_id: string | null;
  ultimo_ts: string; ultimo_de: string | null; sin_leer: number | null;
  prospect: { id: string; full_name: string | null; company_name: string | null;
              company_name_override: string | null } | null;
};

/** Las conversaciones directas, de la más reciente a la más vieja.
 *
 * Sale de `dm_rastro` (0027), que el panel SÍ puede leer: quién, cuándo, quién escribió
 * último y cuánto hay sin leer. El texto no está aquí (ver `leerMensajesDm`). */
export async function listarDms(supabase: Supabase): Promise<Dm[]> {
  const { data } = await supabase.from("dm_rastro")
    .select("dm_id, team_id, slack_user_id, prospect_id, ultimo_ts, ultimo_de, sin_leer, "
      + "prospect:prospects(id, full_name, company_name, company_name_override)")
    .order("ultimo_ts", { ascending: false })
    .limit(200);

  return ((data ?? []) as unknown as FilaRastro[]).map((d) => ({
    kind: "dm" as const,
    id: d.dm_id,
    slackUserId: d.slack_user_id,
    prospectId: d.prospect?.id ?? d.prospect_id ?? null,
    nombre: d.prospect?.full_name ?? null,
    empresa: d.prospect?.company_name_override ?? d.prospect?.company_name ?? null,
    teamId: d.team_id,
    ultimoTs: d.ultimo_ts,
    ultimoDe: d.ultimo_de,
    sinLeer: d.sin_leer,
  }));
}

// ---------------------------------------------------------------------------------
// Los mensajes de un canal
// ---------------------------------------------------------------------------------

// Lo que se enseña de cada mensaje. `status` y `subtype` se traen solo para filtrar.
const CAMPOS = "ts, user_id, text, subtype, thread_ts, status";

// Qué mensaje es de una persona y qué mensaje es ruido del lector.
//
// Es exactamente el filtro de signals/procesar.py: entran los que no están `ignorado`
// y, de los `ignorado`, los que tienen autor y no tienen subtipo de sistema -- en la
// práctica los de AG, que el watcher marca `ignorado` para no investigarse a sí mismo
// (ingest/watcher.py, classify). Quedan fuera los bots y los avisos de sistema
// (uniones, borrados), que llevan subtipo o no tienen autor.
const SOLO_PERSONAS = "status.neq.ignorado,and(user_id.not.is.null,subtype.is.null)";

type FilaMensaje = {
  ts: string; user_id: string | null; text: string | null;
  subtype: string | null; thread_ts: string | null; status: string;
};

// Una fila es principal si no cuelga de ningún hilo, o si es la cabeza del suyo (0032).
const esPrincipal = (m: FilaMensaje) => !m.thread_ts || m.thread_ts === m.ts;

export type Pagina = {
  // Principales de más antiguo a más nuevo, cada uno con sus respuestas.
  mensajes: Mensaje[];
  // Si queda historial por detrás de esta ventana.
  hayMas: boolean;
  // Cuántos mensajes se han leído para armar esta página (principales + respuestas).
  leidos: number;
};

export const LIMITE_POR_OMISION = 100;
// Tope duro: la ventana crece de 100 en 100 con "Load earlier messages", pero no sin fin.
export const LIMITE_MAXIMO = 1000;
// Tope de respuestas que se traen para los hilos de la ventana.
const MAX_RESPUESTAS = 500;

/** Los `limite` mensajes más recientes de un canal, en forma de hilos.
 *
 * El orden es por `ts`, que es la hora real del mensaje en Slack y no cuándo lo leímos
 * (`created_at`): un hilo viejo con una respuesta nueva no debe colarse al final. `ts`
 * es un epoch con seis decimales y siempre la misma anchura, así que ordenarlo como
 * texto da el mismo resultado que `ts::numeric`, que es como lo ordena el worker; está
 * comprobado contra la base.
 *
 * La ventana cuenta mensajes, principales y respuestas juntos, porque PostgREST no
 * compara dos columnas entre sí (`thread_ts = ts`) y separarlos exigiría una vista.
 * Luego se rellena lo que falte para que nada quede huérfano:
 *   1. los principales de las respuestas que entraron sin su cabeza, y
 *   2. todas las respuestas de los principales a la vista, para que el recuento del
 *      hilo sea el de verdad y no solo lo que cayó dentro de la ventana. */
export async function leerCanal(
  supabase: Supabase,
  channelId: string,
  limite: number,
): Promise<{ pagina: Pagina; error: string | null }> {
  const { data, error } = await supabase.from("slack_messages")
    .select(CAMPOS)
    .eq("channel_id", channelId)
    .or(SOLO_PERSONAS)
    .order("ts", { ascending: false })
    .limit(limite + 1);
  if (error) return { pagina: { mensajes: [], hayMas: false, leidos: 0 }, error: error.message };

  const filas = (data ?? []) as FilaMensaje[];
  const hayMas = filas.length > limite;
  const ventana = hayMas ? filas.slice(0, limite) : filas;

  const principales = new Map<string, FilaMensaje>();
  for (const m of ventana) if (esPrincipal(m)) principales.set(m.ts, m);

  // (1) Las respuestas de la ventana cuyo principal se quedó fuera: sin él no hay
  // contexto, así que se traen aunque sean viejos (pueden estar `archivado`).
  const huerfanos = [...new Set(
    ventana.filter((m) => !esPrincipal(m) && !principales.has(m.thread_ts!))
      .map((m) => m.thread_ts!),
  )];
  if (huerfanos.length > 0) {
    const { data: cabezas } = await supabase.from("slack_messages")
      .select(CAMPOS)
      .eq("channel_id", channelId)
      .or(SOLO_PERSONAS)
      .in("ts", huerfanos);
    for (const m of (cabezas ?? []) as FilaMensaje[]) principales.set(m.ts, m);
  }

  // (2) Todas las respuestas de esos hilos. Sin esto, "3 replies" significaría "3 de
  // las que caben en la ventana", que no es lo que nadie entiende al leerlo.
  const respuestasPorHilo = new Map<string, FilaMensaje[]>();
  const tsPrincipales = [...principales.keys()];
  let leidos = ventana.length;
  if (tsPrincipales.length > 0) {
    const { data: respuestas } = await supabase.from("slack_messages")
      .select(CAMPOS)
      .eq("channel_id", channelId)
      .or(SOLO_PERSONAS)
      .in("thread_ts", tsPrincipales)
      .order("ts", { ascending: true })
      .limit(MAX_RESPUESTAS);
    for (const m of (respuestas ?? []) as FilaMensaje[]) {
      if (esPrincipal(m)) continue;
      const lista = respuestasPorHilo.get(m.thread_ts!) ?? [];
      lista.push(m);
      respuestasPorHilo.set(m.thread_ts!, lista);
      leidos += 1;
    }
  }

  const mensajes: Mensaje[] = [...principales.values()]
    .sort((a, b) => a.ts.localeCompare(b.ts))
    .map((m) => ({
      ts: m.ts,
      userId: m.user_id,
      texto: m.text,
      respuestas: (respuestasPorHilo.get(m.ts) ?? []).map((r) => ({
        ts: r.ts, userId: r.user_id, texto: r.text, respuestas: [],
      })),
    }));

  return { pagina: { mensajes, hayMas, leidos }, error: null };
}

// ---------------------------------------------------------------------------------
// El texto de un DM: la única puerta, y hoy está cerrada
// ---------------------------------------------------------------------------------

export type MensajesDm =
  // `dm_texto_activo` apagado: no hay nada guardado que leer (0031).
  | { estado: "apagado" }
  // Encendido, pero el panel no tiene permiso sobre `dm_mensajes`. Hace falta una
  // migración posterior; ver el informe y el aviso que pinta la página.
  | { estado: "sin_permiso"; detalle: string }
  | { estado: "ok"; mensajes: Mensaje[] };

/** El texto de una conversación directa.
 *
 * Es la ÚNICA función que lee el texto de un DM, a propósito: el día que una migración
 * dé `select` sobre `dm_mensajes` al rol del panel con una política `is_panel_user()`,
 * esto empieza a devolver filas y no hay que cambiar nada más.
 *
 * Hoy devuelve 'apagado' (el interruptor está en false en producción) o 'sin_permiso':
 * `dm_mensajes` se creó en 0031 SIN permisos ni política para el panel, de modo que la
 * consulta falla. No se arregla desde aquí, y no se intenta. */
export async function leerMensajesDm(
  supabase: Supabase,
  dmId: string,
  interruptores: Interruptores,
): Promise<MensajesDm> {
  if (!interruptores.dmTexto) return { estado: "apagado" };

  const { data, error } = await supabase.from("dm_mensajes")
    .select("ts, de, texto")
    .eq("dm_id", dmId)
    .order("ts", { ascending: true })
    .limit(LIMITE_MAXIMO);
  // Sin permisos, PostgREST contesta error (la tabla no está ni en su caché de esquema).
  if (error) return { estado: "sin_permiso", detalle: error.message };

  return {
    estado: "ok",
    mensajes: ((data ?? []) as { ts: string; de: string; texto: string }[]).map((m) => ({
      // `de` es 'dueno' u 'otro', no un id de Slack: la página lo traduce a un nombre.
      ts: m.ts, userId: m.de, texto: m.texto, respuestas: [],
    })),
  };
}

// ---------------------------------------------------------------------------------
// Quién escribió cada cosa
// ---------------------------------------------------------------------------------

/** Los autores de un puñado de mensajes, por su id de Slack.
 *
 * Mucha gente del club no tiene fila en `prospects` (nunca pasó por el research): para
 * esos no hay nombre ni ficha y se enseña el id de Slack, que es lo único que hay. */
export async function leerAutores(
  supabase: Supabase,
  slackUserIds: string[],
): Promise<Map<string, Autor>> {
  const ids = [...new Set(slackUserIds)];
  if (ids.length === 0) return new Map();

  const { data } = await supabase.from("prospects")
    .select("id, slack_user_id, full_name, company_name, company_name_override")
    .in("slack_user_id", ids);

  return new Map((data ?? []).map((p) => [p.slack_user_id as string, {
    slackUserId: p.slack_user_id as string,
    nombre: (p.full_name as string | null) ?? null,
    empresa: (p.company_name_override as string | null) ?? (p.company_name as string | null) ?? null,
    prospectId: p.id as string,
  }]));
}

/** Todos los ids de Slack que aparecen en una página de mensajes. */
export function autoresDe(mensajes: Mensaje[]): string[] {
  const ids: string[] = [];
  for (const m of mensajes) {
    if (m.userId) ids.push(m.userId);
    for (const r of m.respuestas) if (r.userId) ids.push(r.userId);
  }
  return ids;
}
