import { createClient } from "@/lib/supabase/server";
import { canalLabel, fecha, minutosDesde } from "@/lib/format";
import {
  guardarCanalesTodos, guardarCanalPresentaciones, guardarChatResponderActivo,
  guardarExigirEvidencia, guardarHubspotActivo,
  guardarDmTextoActivo, guardarEnlaceAppActivo, guardarLeerHilos, guardarPausaUnipile, guardarPrioridadInterruptor,
  guardarReporteInterruptor, guardarSaludoSugeridoActivo, guardarSeguimientoActivo, guardarSeguimientoInterruptor,
  guardarUmbral,
} from "../acciones";
import { CanalesForm } from "./CanalesForm";
import { ConectarClaude, type SesionOauth, type TokenMcp } from "./ConectarClaude";
import { TokenHubspotForm } from "./TokenHubspotForm";
import { TokenSlackForm } from "./TokenSlackForm";

const EDITABLES: Record<string, string> = {
  banda_alta_min: "Minimum fit score for the high band (card + SMS)",
  banda_media_min: "Minimum fit score for the medium band (card)",
  banda_baja_min: "Minimum fit score for the low band (daily email). Raise it to silence.",
  research_por_hora: "Maximum research jobs per hour",
  sms_por_dia: "Maximum SMS per day",
  sms_hora_inicio: "Hour from which SMS are allowed",
  sms_hora_fin: "Hour until which SMS are allowed",
  mcp_llamadas_por_dia: "Maximum remote-MCP tool calls per token per day (0 switches it off)",
  mcp_escrituras_por_dia: "Maximum changes Claude can make per person per day (0 switches writes off)",
};

// URL pública del MCP remoto: el dominio del receptor (el servicio web de Railway,
// el mismo que recibe los botones de Slack) más /mcp. Se puede cambiar con
// MCP_PUBLIC_URL si el dominio cambia, sin tocar código.
const MCP_URL = process.env.MCP_PUBLIC_URL ?? "https://buttons-slack-production.up.railway.app/mcp";

// Recordatorios de los mensajes directos (0027_seguimiento_dm.sql): los
// numéricos que la política panel_ajusta_seguimiento deja tocar.
// seguimiento_activo va aparte porque es un booleano.
const EDITABLES_SEGUIMIENTO: Record<string, string> = {
  seguimiento_horas_para_responder: "Hours before \"they replied, you haven't\" counts as waiting",
  seguimiento_dias_sin_respuesta: "Days with no answer before it shows up as waiting",
  seguimiento_max_por_resumen: "Maximum lines in the daily follow-up summary",
  seguimiento_retencion_dias: "Days a conversation stays on file after its last message",
  seguimiento_recordar_horas:
    "Hours before a conversation that is still waiting is brought up again",
  seguimiento_avisos_por_dia: "Maximum follow-up alerts posted per day",
  // Sugerencias de respuesta (0031): los numéricos que deja tocar la política
  // panel_ajusta_dm_texto. dm_texto_activo va aparte porque es un booleano.
  dm_texto_retencion_dias: "Days the text of a conversation is kept for reply suggestions",
  dm_sugerencias_por_dia: "Maximum reply suggestions per day",
};

// Recordatorios de hilos, menciones y gente sin ficha (0038_seguimiento_canal.sql):
// los numéricos que la política panel_ajusta_seguimiento_canal deja tocar. Los tres
// interruptores van aparte porque son booleanos.
const EDITABLES_SEGUIMIENTO_CANAL: Record<string, string> = {
  seguimiento_mencion_horas: "Hours a mention waits before AG is reminded to answer it",
  seguimiento_hilo_horas: "Hours a thread reply waits before AG is reminded to answer it",
  seguimiento_canal_max_avisos:
    "Maximum reminders for the same thread or mention (after that, only the daily summary)",
  seguimiento_caduca_dias:
    "Days after which a thread, a mention or a non-lead conversation is no longer brought up",
};

// Los tres interruptores de 0038: la clave, el texto y si nace encendido. Los dos de
// canal valen true salvo que la base diga false; el de gente sin ficha, al revés.
const INTERRUPTORES_SEGUIMIENTO: [string, string, boolean][] = [
  ["seguimiento_menciones_activo",
    "Unanswered mentions. Someone mentioned AG and he has not written back since (in that " +
    "thread, or in that channel) and has not reacted to it. Checked against Slack right " +
    "before every reminder.", true],
  ["seguimiento_hilos_activo",
    "Unanswered thread replies. Someone replied in a thread AG started, or the person who " +
    "started a thread answered him there, and he has not written in that thread since. " +
    "Needs \"Read thread replies\" to be on.", true],
  ["seguimiento_sin_ficha_activo",
    "Direct messages from people who are not leads yet. Off by default: turned on, the " +
    "cards channel will also show who is waiting for AG among people the agent has not " +
    "researched. Still no text is read or stored, only who wrote last and when.", false],
];

// La prioridad de los avisos y el resumen del día (0039_prioridad.sql): los numéricos
// que la política panel_ajusta_prioridad deja tocar. Los dos interruptores van aparte
// porque son booleanos.
const EDITABLES_PRIORIDAD: Record<string, string> = {
  resumen_prioridad_hora:
    "Hour of the day (0–23) from which the daily brief is posted, in the same time zone as the SMS hours",
  resumen_prioridad_max_por_nivel:
    "Maximum lines per priority level in the daily brief (the rest is counted, not listed)",
  resumen_prioridad_dias:
    "Days a card or a \"they asked\" nobody has marked stays in the daily brief",
};

// Los dos interruptores de 0039: la clave y el texto. Los dos valen true salvo que la
// base diga false, igual que en el worker (delivery/prioridad.py y
// delivery/resumen_prioridad.py).
const INTERRUPTORES_PRIORIDAD: [string, string][] = [
  ["prioridad_activa",
    "Priority label on every alert. Each alert in the cards channel, and its phone " +
    "notification, starts with how urgent it is: ACT NOW, TODAY or FYI. Turn it off and " +
    "alerts look the way they did before."],
  ["resumen_prioridad_activo",
    "Daily brief. One message a day with everything still open, most urgent first. It " +
    "includes the follow-ups, so it replaces the separate daily follow-up summary: turn " +
    "it off and that summary comes back instead. Either way it is one summary a day."],
];

// Una tarjeta por persona y el reporte semanal de empresas contratando
// (0040_repetidas_y_reporte_semanal.sql): los numéricos que la política
// panel_ajusta_repetidas_y_reporte deja tocar. Los dos interruptores van aparte porque
// son booleanos.
const EDITABLES_REPETIDAS: Record<string, string> = {
  tarjeta_dias_misma_persona:
    "Days before the same person can get another card or SMS (sooner only if their fit " +
    "went up or someone asked for new research; 0 switches this off)",
  research_reintento_horas:
    "Hours before the agent retries, on its own, a research that came out incomplete",
  research_reintentos_max:
    "Maximum automatic retries of an incomplete research (after that, only by hand)",
};

const EDITABLES_REPORTE: Record<string, string> = {
  reporte_contratando_dia:
    "Day of the week the hiring report is posted (1 = Monday … 7 = Sunday)",
  reporte_contratando_hora:
    "Hour of that day (0–23) from which it is posted, in the same time zone as the SMS hours",
  reporte_contratando_dias: "Days back the report looks for companies with open roles",
  reporte_contratando_max: "Maximum companies in one report (the rest is counted, not listed)",
};

// Los dos interruptores de 0040: la clave y el texto. Los dos valen true salvo que la
// base diga false, igual que en el worker (delivery/bands.py y
// delivery/reporte_contratando.py).
const INTERRUPTORES_REPORTE: [string, string][] = [
  ["vacantes_solo_reporte",
    "Hiring-only companies skip the cards. When the only evidence is that a company " +
    "posted open roles, and the person did not ask for anything, there is no HIGH or " +
    "MEDIUM SIGNAL card and no SMS: they go to the weekly hiring report instead. Turn " +
    "it off and they come as cards again, like before."],
  ["reporte_contratando_activo",
    "Weekly hiring report. One message a week in the cards channel listing the " +
    "companies hiring right now: the member, their role, what they are hiring for and " +
    "a LinkedIn link. Each company is listed once."],
];

// Radar de cuentas objetivo (0012_radar_cuentas.sql): los numéricos que la
// política panel_ajusta_radar deja tocar. unipile_pausado va aparte porque
// es un booleano.
// Avisos de canal (0030_eventos_canal.sql): no cuestan una llamada al modelo, así que
// sus topes son altos. canal_presentaciones va aparte porque es una cadena.
const EDITABLES_EVENTOS: Record<string, string> = {
  menciones_por_dia: "Maximum \"you were mentioned\" alerts per day",
  presentaciones_por_dia: "Maximum \"new introduction\" alerts per day",
  // El plazo de 0041_enlace_app_y_tope_eventos.sql (política panel_ajusta_enlace_app,
  // de 1 a 168): lo que no salió a tiempo se cierra sin publicar, así que subir un tope
  // nunca suelta un atraso.
  eventos_edad_maxima_horas:
    "Hours after which a mention or introduction that was never posted is dropped " +
    "instead of posted late (1–168). Raising a cap never releases a backlog",
  // El aviso de miembro nuevo (0037_saludos_y_miembros.sql): los numéricos que deja
  // tocar la política panel_ajusta_saludos. saludo_sugerido_activo va aparte porque es
  // un booleano.
  miembros_nuevos_por_dia: "Maximum \"new member\" alerts per day (0 switches them off)",
  miembro_nuevo_espera_min:
    "Minutes a \"new member\" alert waits for the research before going out without it",
};

// Respuestas de los hilos (0032_hilos.sql): los numéricos que la política
// panel_ajusta_hilos deja tocar. leer_hilos va aparte porque es un booleano.
const EDITABLES_HILOS: Record<string, string> = {
  hilos_dias: "Days back the agent looks for new replies in older threads",
  hilos_por_vuelta: "Maximum threads read in one poll",
};

// Contestar desde el panel (0034_chat_panel.sql): el numérico que la política
// panel_ajusta_chat deja tocar. chat_responder_activo va aparte porque es un booleano.
const EDITABLES_CHAT: Record<string, string> = {
  chat_mensajes_por_dia: "Maximum messages sent from the panel per day (everyone together)",
};

// El CRM (0035_hubspot.sql): el numérico que la política panel_ajusta_hubspot
// deja tocar. hubspot_activo va aparte porque es un booleano.
const EDITABLES_HUBSPOT: Record<string, string> = {
  hubspot_por_dia: "Maximum people synced to HubSpot per day",
};

const EDITABLES_RADAR: Record<string, string> = {
  radar_auto_min: "Minimum score for a role to be pursued automatically (0–10)",
  radar_horas_entre_escaneos: "Hours between scans of the same account",
  unipile_busquedas_por_dia: "Maximum LinkedIn (Unipile) searches per day",
  unipile_perfiles_por_dia: "Maximum LinkedIn (Unipile) profile lookups per day",
};

// Latido del lector de Slack (0018_estado_lector.sql): lo escribe el worker en
// cada sondeo. Pasado este tiempo sin latido, "ok" ya no es de fiar.
const LATIDO_CADUCADO_MIN = 15;

type Latido = {
  ok?: boolean; en?: string; usuario?: string; equipo?: string; canales?: number;
  error?: string; detalle?: string;
};

// Medianoche de hoy en la zona de Unipile, como instante UTC: el mismo "hoy"
// con el que el worker cuenta sus búsquedas (tools/unipile.py, _usadas_hoy).
function inicioDelDia(zona: string): string {
  const ahora = new Date();
  let partes: Record<string, number>;
  try {
    partes = Object.fromEntries(
      new Intl.DateTimeFormat("en-US", {
        timeZone: zona, hourCycle: "h23",
        year: "numeric", month: "numeric", day: "numeric",
        hour: "numeric", minute: "numeric", second: "numeric",
      }).formatToParts(ahora)
        .filter((p) => p.type !== "literal")
        .map((p) => [p.type, Number(p.value)]),
    );
  } catch {
    // Zona inválida en config: se cuenta desde la medianoche UTC.
    return new Date(Date.UTC(ahora.getUTCFullYear(), ahora.getUTCMonth(), ahora.getUTCDate())).toISOString();
  }
  const comoUtc = Date.UTC(partes.year, partes.month - 1, partes.day, partes.hour, partes.minute, partes.second);
  const desfase = comoUtc - Math.floor(ahora.getTime() / 1000) * 1000;
  return new Date(Date.UTC(partes.year, partes.month - 1, partes.day) - desfase).toISOString();
}

export default async function Ajustes() {
  const supabase = await createClient();
  const { data } = await supabase.from("config").select("key, value, updated_at")
    .in("key", [...Object.keys(EDITABLES), ...Object.keys(EDITABLES_RADAR),
      ...Object.keys(EDITABLES_SEGUIMIENTO), ...Object.keys(EDITABLES_EVENTOS),
      ...Object.keys(EDITABLES_SEGUIMIENTO_CANAL),
      ...INTERRUPTORES_SEGUIMIENTO.map(([clave]) => clave),
      ...Object.keys(EDITABLES_PRIORIDAD),
      ...INTERRUPTORES_PRIORIDAD.map(([clave]) => clave),
      ...Object.keys(EDITABLES_REPETIDAS), ...Object.keys(EDITABLES_REPORTE),
      ...INTERRUPTORES_REPORTE.map(([clave]) => clave),
      ...Object.keys(EDITABLES_HILOS), "leer_hilos",
      ...Object.keys(EDITABLES_CHAT), "chat_responder_activo",
      ...Object.keys(EDITABLES_HUBSPOT), "hubspot_activo", "hubspot_desde",
      "canal_presentaciones", "saludo_sugerido_activo", "seguimiento_activo", "dm_texto_activo",
      "enlace_app_activo",
      "unipile_pausado", "unipile_zona", "slack_channel_ids",
      "lector_estado", "exigir_evidencia", "slack_canales_todos"]);
  const valores = new Map((data ?? []).map((r) => [r.key, r.value]));
  const canalesValor = valores.get("slack_channel_ids");
  const canales = Array.isArray(canalesValor)
    ? canalesValor.filter((c): c is string => typeof c === "string")
    : [];
  // Los nombres que el worker ya averiguó (conversations.info, solo lectura): un código
  // de canal no le dice nada a nadie.
  const { data: nombresData } = await supabase.from("canales")
    .select("channel_id, nombre, es_miembro, mensajes_vistos, sondeado_at");
  const nombrePorCanal = new Map(
    (nombresData ?? []).map((c) => [c.channel_id as string, c.nombre as string | null]),
  );
  const canalVisible = (id: string) => canalLabel(id, nombrePorCanal.get(id), true);
  const latidoValor = valores.get("lector_estado");
  const latido: Latido | null =
    latidoValor && typeof latidoValor === "object" && !Array.isArray(latidoValor) &&
    typeof (latidoValor as Latido).en === "string"
      ? (latidoValor as Latido)
      : null;
  const latidoCaducado = latido?.ok === true && latido.en !== undefined &&
    minutosDesde(latido.en) > LATIDO_CADUCADO_MIN;
  const pausado = valores.get("unipile_pausado") === true;
  // Activado salvo que la base diga false: el mismo valor por defecto que usa
  // el worker (delivery/bands.py) cuando la clave falta o no es un booleano.
  const exigirEvidencia = valores.get("exigir_evidencia") !== false;
  // Activado salvo que la base diga false, igual que en el worker (ingest/dms.py).
  const seguimientoActivo = valores.get("seguimiento_activo") !== false;
  // Apagado salvo que la base diga true, igual que en el worker (ingest/dm_texto.py):
  // leer el texto de sus conversaciones solo pasa si alguien lo enciende.
  const dmTextoActivo = valores.get("dm_texto_activo") === true;
  // Apagado salvo que la base diga true: el mismo valor por defecto que el worker
  // (canales.modo_todos()). Encendido, los canales salen del descubrimiento.
  const canalesTodos = valores.get("slack_canales_todos") === true;
  // Apagado salvo que la base diga true, igual que en el worker (ingest/watcher.py).
  const leerHilos = valores.get("leer_hilos") === true;
  // Apagado salvo que la base diga true, igual que en el worker
  // (tools/respondedor._activo). Nace apagado en 0034: escribir en el club ajeno no
  // puede ser el estado por omisión de nada.
  const chatResponderActivo = valores.get("chat_responder_activo") === true;
  // Apagado salvo que la base diga true, igual que en el worker
  // (crm/hubspot_sync.py). Nace apagado: escribir en el CRM del equipo
  // comercial no puede ser el valor por omisión de nada.
  const hubspotActivo = valores.get("hubspot_activo") === true;
  // La marca desde la que se sincroniza: vacía hasta que el worker hace su
  // primera pasada y escribe `now()` ahí. Nada anterior entra solo.
  const hubspotDesde = String(valores.get("hubspot_desde") ?? "");
  const canalIntros = String(valores.get("canal_presentaciones") ?? "");
  // Encendido salvo que la base diga false, igual que en el worker
  // (delivery/saludo.py): nace encendido en 0037.
  const saludoSugerido = valores.get("saludo_sugerido_activo") !== false;
  // Igual: nace encendido en 0041 (delivery/enlace_app.py).
  const enlaceApp = valores.get("enlace_app_activo") !== false;
  const descubiertos = (nombresData ?? []).filter((c) => c.es_miembro === true);
  const masActivos = [...descubiertos]
    .sort((a, b) => Number(b.mensajes_vistos ?? 0) - Number(a.mensajes_vistos ?? 0))
    .slice(0, 8);
  const zona = String(valores.get("unipile_zona") ?? "America/New_York");
  const desde = inicioDelDia(zona);

  const [busquedas, perfiles] = await Promise.all([
    supabase.from("agent_actions").select("id", { count: "exact", head: true })
      .eq("action", "unipile_busqueda").gte("created_at", desde),
    supabase.from("agent_actions").select("id", { count: "exact", head: true })
      .eq("action", "unipile_perfil").gte("created_at", desde),
  ]);

  const fila = ([key, label]: [string, string]) => (
    <tr key={key}>
      <td>{label}<div className="muted" style={{ fontSize: 12 }}>{key}</div></td>
      <td>
        <form action={guardarUmbral} className="inline">
          <input type="hidden" name="key" value={key} />
          <input name="value" type="number" min={0} step={1} style={{ width: 90 }}
                 defaultValue={String(valores.get(key) ?? "")} />
          <button type="submit">Save</button>
        </form>
      </td>
    </tr>
  );

  // panel_estado_token_slack nunca devuelve el token: solo si hay uno y sus 4 últimos.
  const { data: estado } = await supabase.rpc("panel_estado_token_slack");
  const token = Array.isArray(estado) ? estado[0] : estado;

  // Igual con la llave de HubSpot (0035): nunca el valor, solo si hay una y
  // sus cuatro últimos.
  const { data: estadoHubspot } = await supabase.rpc("panel_estado_token_hubspot");
  const tokenHubspot = Array.isArray(estadoHubspot) ? estadoHubspot[0] : estadoHubspot;

  // panel_tokens_mcp solo devuelve los tokens de quien mira, y nunca su hash.
  const { data: tokensMcp } = await supabase.rpc("panel_tokens_mcp");
  // Y panel_sesiones_oauth (0026), las conexiones hechas desde Claude con el correo y la
  // contraseña, sin copiar ninguna llave. Mismas reglas: solo las propias, nunca un hash.
  const { data: sesionesOauth } = await supabase.rpc("panel_sesiones_oauth");

  return (
    <>
      <h1>Settings</h1>
      <section style={{ marginBottom: 24 }}>
        <h2>Connect your Claude</h2>
        <ConectarClaude
          tokens={(tokensMcp ?? []) as TokenMcp[]}
          sesiones={(sesionesOauth ?? []) as SesionOauth[]}
          url={MCP_URL}
        />
      </section>
      <section style={{ marginBottom: 24 }}>
        <h2>Founders Club reader token</h2>
        <p>
          {token?.configurado
            ? `Configured (${token.tipo === "session" ? "browser session" : "app token"}) · ` +
              `ends in ${token.sufijo} · updated ${fecha(token.actualizado)} ` +
              `by ${token.por ?? "unknown"}`
            : "Not configured — the agent cannot read the Founders Club yet."}
        </p>
        {token?.configurado && token.tipo === "session" && (
          <p className="notice">
            A browser session expires every few days; paste a fresh one when the agent reports it
            can no longer read.
          </p>
        )}
        {!latido ? (
          <p className="muted">The worker has not polled yet.</p>
        ) : latido.ok ? (
          <p>
            Last successful read: {fecha(latido.en)}
            {latido.equipo ? ` · workspace ${latido.equipo}` : ""}
            {typeof latido.canales === "number"
              ? ` · ${latido.canales} ${latido.canales === 1 ? "channel" : "channels"}`
              : ""}
            {latidoCaducado && " (stale — the worker may be down)"}
          </p>
        ) : (
          <p className="notice error">
            Reading failed {fecha(latido.en)} — {latido.error ?? "unknown error"}. If the session
            expired, paste a fresh token and cookie.
          </p>
        )}
        <TokenSlackForm configurado={Boolean(token?.configurado)} />
      </section>
      <section style={{ marginBottom: 24 }}>
        <h2>Watched channels</h2>
        <table>
          <tbody>
            <tr>
              <td>Watch every channel the reader account belongs to, and pick up new ones
                on its own. Busy channels are read every couple of minutes and quiet ones
                much less often, so adding channels costs very little. The agent never
                joins a channel: it only reads the ones the account is already in.
                <div className="muted" style={{ fontSize: 12 }}>slack_canales_todos</div></td>
              <td>
                <form action={guardarCanalesTodos} className="inline">
                  <input type="hidden" name="value" value={canalesTodos ? "false" : "true"} />
                  <span>{canalesTodos ? "On" : "Off"}</span>
                  <button type="submit">{canalesTodos ? "Turn off" : "Turn on"}</button>
                </form>
              </td>
            </tr>
            <tr>
              <td>Read thread replies. The agent reads replies inside threads, with the
                whole thread as context, so it can find people who say they need the same
                thing. It only reads, never writes.
                <div className="muted" style={{ fontSize: 12 }}>leer_hilos</div></td>
              <td>
                <form action={guardarLeerHilos} className="inline">
                  <input type="hidden" name="value" value={leerHilos ? "false" : "true"} />
                  <span>{leerHilos ? "On" : "Off"}</span>
                  <button type="submit">{leerHilos ? "Turn off" : "Turn on"}</button>
                </form>
              </td>
            </tr>
            {Object.entries(EDITABLES_HILOS).map(fila)}
          </tbody>
        </table>
        {canalesTodos ? (
          <>
            <p>
              {descubiertos.length > 0
                ? `Watching ${descubiertos.length} ${
                    descubiertos.length === 1 ? "channel" : "channels"
                  } — busiest: ${masActivos.map((c) => canalVisible(c.channel_id)).join(", ")}`
                : "The worker has not discovered the channels yet — this takes one poll."}
            </p>
            <p className="notice">
              The list below is not in use while this is on. It is kept as it is and takes
              over again if you turn it off.
            </p>
          </>
        ) : (
          <p>
            {canales.length > 0
              ? canales.map(canalVisible).join(", ")
              : "No channels configured — the agent is not reading anything"}
          </p>
        )}
        <CanalesForm key={canales.join(",")} actuales={canales} />
      </section>
      <p className="notice">The emergency shutdown and the money caps aren&apos;t changed from here,
        on purpose: they&apos;re edited by hand in the database.</p>
      <table>
        <tbody>
          {Object.entries(EDITABLES).map(fila)}
          <tr>
            <td>Require evidence for a card. When on, a fit score with no concrete evidence
              behind it (no open roles, no need stated by the person, no relevant team) stays
              in the daily email whatever the score: no card, no SMS.
              <div className="muted" style={{ fontSize: 12 }}>exigir_evidencia</div></td>
            <td>
              <form action={guardarExigirEvidencia} className="inline">
                <input type="hidden" name="value" value={exigirEvidencia ? "false" : "true"} />
                <span>{exigirEvidencia ? "On" : "Off"}</span>
                <button type="submit">{exigirEvidencia ? "Turn off" : "Turn on"}</button>
              </form>
            </td>
          </tr>
        </tbody>
      </table>

      <h2>Priorities and the daily brief</h2>
      <p>
        Every alert is given one of three levels so it is clear what to handle first:{" "}
        <b>ACT NOW</b> (a likely customer is waiting: a high signal, a clear request, a hot
        lead waiting for a reply), <b>TODAY</b> (worth a few minutes before the day ends: a
        medium signal, a mention, an introduction) and <b>FYI</b> (good to know, nothing
        waiting on you). The levels follow fixed rules, not a model. Nothing here changes
        how many alerts are sent.
      </p>
      <table>
        <tbody>
          {INTERRUPTORES_PRIORIDAD.map(([clave, texto]) => {
            const activo = valores.get(clave) !== false;
            return (
              <tr key={clave}>
                <td>{texto}<div className="muted" style={{ fontSize: 12 }}>{clave}</div></td>
                <td>
                  <form action={guardarPrioridadInterruptor} className="inline">
                    <input type="hidden" name="key" value={clave} />
                    <input type="hidden" name="value" value={activo ? "false" : "true"} />
                    <span>{activo ? "On" : "Off"}</span>
                    <button type="submit">{activo ? "Turn off" : "Turn on"}</button>
                  </form>
                </td>
              </tr>
            );
          })}
          {Object.entries(EDITABLES_PRIORIDAD).map(fila)}
          <tr>
            <td>&quot;Open in app&quot; link. Next to the usual link to the original Founders
              Club message (which opens the browser), every alert carries a second link
              that opens the same message inside the Slack app — the one to use on a
              phone. Turn it off and the alerts go back to the web link only.
              <div className="muted" style={{ fontSize: 12 }}>enlace_app_activo</div></td>
            <td>
              <form action={guardarEnlaceAppActivo} className="inline">
                <input type="hidden" name="value" value={enlaceApp ? "false" : "true"} />
                <span>{enlaceApp ? "On" : "Off"}</span>
                <button type="submit">{enlaceApp ? "Turn off" : "Turn on"}</button>
              </form>
            </td>
          </tr>
        </tbody>
      </table>

      <h2>One card per person, and the weekly hiring report</h2>
      <p>
        A person gets one research card. If the agent researches them again, a second
        card only comes when there is a real reason: their fit went up, or someone on the
        team asked for new research. And a company that is only hiring is not a signal to
        act on today, so those come together once a week, as a list for nurturing (open
        their LinkedIn, comment on a post).
      </p>
      <table>
        <tbody>
          {Object.entries(EDITABLES_REPETIDAS).map(fila)}
          {INTERRUPTORES_REPORTE.map(([clave, texto]) => {
            const activo = valores.get(clave) !== false;
            return (
              <tr key={clave}>
                <td>{texto}<div className="muted" style={{ fontSize: 12 }}>{clave}</div></td>
                <td>
                  <form action={guardarReporteInterruptor} className="inline">
                    <input type="hidden" name="key" value={clave} />
                    <input type="hidden" name="value" value={activo ? "false" : "true"} />
                    <span>{activo ? "On" : "Off"}</span>
                    <button type="submit">{activo ? "Turn off" : "Turn on"}</button>
                  </form>
                </td>
              </tr>
            );
          })}
          {Object.entries(EDITABLES_REPORTE).map(fila)}
        </tbody>
      </table>

      <h2>Channel alerts</h2>
      <p>
        Three alerts posted in the cards channel, each with its own look: when someone
        mentions you in a watched channel, when someone posts in the introductions
        channel (a reminder to say hi, with a LinkedIn link), and when someone new
        joins a watched channel (a reminder to welcome them). The agent never writes
        in the Founders Club: the greeting is text for you to copy.
      </p>
      <section style={{ marginBottom: 16 }}>
        <form action={guardarCanalPresentaciones} className="inline">
          <label>
            Introductions channel{" "}
            <input name="value" defaultValue={canalIntros} placeholder="C0…"
                   style={{ width: 140 }} />
          </label>
          <button type="submit">Save</button>
        </form>
        <p className="muted" style={{ fontSize: 12 }}>
          Leave it empty and no introduction alerts are sent. The channel is not guessed
          from its name: getting that wrong would mean an alert for everything posted in
          some other channel.
        </p>
      </section>
      <table>
        <tbody>
          <tr>
            <td>Suggested greeting. When someone introduces themselves, the alert comes
              with a short greeting written from what they said, ready to copy. It costs
              one small model call per introduction. Turn it off and the alert still
              arrives, without the suggestion.
              <div className="muted" style={{ fontSize: 12 }}>saludo_sugerido_activo</div></td>
            <td>
              <form action={guardarSaludoSugeridoActivo} className="inline">
                <input type="hidden" name="value" value={saludoSugerido ? "false" : "true"} />
                <span>{saludoSugerido ? "On" : "Off"}</span>
                <button type="submit">{saludoSugerido ? "Turn off" : "Turn on"}</button>
              </form>
            </td>
          </tr>
          {Object.entries(EDITABLES_EVENTOS).map(fila)}
        </tbody>
      </table>

      <h2>Direct message follow-ups</h2>
      <p>
        The agent keeps track of which one-to-one conversations in the Founders Club are
        waiting — who wrote last and when — and posts one summary a day in the cards channel.
        It never writes there. Unless reply suggestions are turned on below, it{" "}
        <b>never stores what was said</b>: only the timing.
      </p>
      <table>
        <tbody>
          <tr>
            <td>Follow-up reminders. Turn this off and the agent stops reading direct
              messages altogether.
              <div className="muted" style={{ fontSize: 12 }}>seguimiento_activo</div></td>
            <td>
              <form action={guardarSeguimientoActivo} className="inline">
                <input type="hidden" name="value" value={seguimientoActivo ? "false" : "true"} />
                <span>{seguimientoActivo ? "On" : "Off"}</span>
                <button type="submit">{seguimientoActivo ? "Turn off" : "Turn on"}</button>
              </form>
            </td>
          </tr>
          <tr>
            <td>Reply suggestions. Turn this on and the agent reads the text of AG&apos;s
              direct messages with leads (people it has already researched), so it can
              suggest what to reply. Other conversations are never opened, and reading
              never marks anything as read. It only reads while follow-up reminders are on
              too. Turn it off and everything stored is deleted within 15 minutes.
              <div className="muted" style={{ fontSize: 12 }}>dm_texto_activo</div></td>
            <td>
              <form action={guardarDmTextoActivo} className="inline">
                <input type="hidden" name="value" value={dmTextoActivo ? "false" : "true"} />
                <span>{dmTextoActivo ? "On" : "Off"}</span>
                <button type="submit">{dmTextoActivo ? "Turn off" : "Turn on"}</button>
              </form>
            </td>
          </tr>
          {Object.entries(EDITABLES_SEGUIMIENTO).map(fila)}
        </tbody>
      </table>

      <h2>Reminders for threads, mentions and people who are not leads yet</h2>
      <p>
        The same reminders, for what direct messages with leads do not cover. They use the
        same daily limit and the same &ldquo;waiting on you&rdquo; and follow-up posts. Only
        what happens after a switch was last turned on counts: nothing older is brought up.
        Reacting with an emoji counts as answering.
      </p>
      <table>
        <tbody>
          {INTERRUPTORES_SEGUIMIENTO.map(([clave, texto, porDefecto]) => {
            const valor = valores.get(clave);
            const activo = porDefecto ? valor !== false : valor === true;
            return (
              <tr key={clave}>
                <td>{texto}<div className="muted" style={{ fontSize: 12 }}>{clave}</div></td>
                <td>
                  <form action={guardarSeguimientoInterruptor} className="inline">
                    <input type="hidden" name="key" value={clave} />
                    <input type="hidden" name="value" value={activo ? "false" : "true"} />
                    <span>{activo ? "On" : "Off"}</span>
                    <button type="submit">{activo ? "Turn off" : "Turn on"}</button>
                  </form>
                </td>
              </tr>
            );
          })}
          {Object.entries(EDITABLES_SEGUIMIENTO_CANAL).map(fila)}
        </tbody>
      </table>

      <h2>Replying from the panel</h2>
      <p>
        Until now the agent could not write a single word in the Founders Club. This lets
        one person answer on AG&apos;s behalf while he is away. Every message is typed by a
        person and sent by hand &mdash; there is no automatic reply, no suggestion that
        sends itself. Only people with the <b>Reply</b> permission can send; everyone else
        sees the chat and cannot write in it. Messages can only go to channels the agent
        already watches and to conversations that already exist, never to anyone new. Every
        message is recorded with the name of whoever actually sent it.
      </p>
      <table>
        <tbody>
          <tr>
            <td>
              Reply in the Founders Club as Anthony. Messages sent from the panel are
              posted in the Founders Club under <b>Anthony&apos;s own name</b>, from his
              Slack session &mdash; members see them as written by him. Turn this off and
              the chat is read-only.
              <div className="muted" style={{ fontSize: 12 }}>chat_responder_activo</div>
            </td>
            <td>
              <form action={guardarChatResponderActivo} className="inline">
                <input type="hidden" name="value" value={chatResponderActivo ? "false" : "true"} />
                <span>{chatResponderActivo ? "On" : "Off"}</span>
                <button type="submit">{chatResponderActivo ? "Turn off" : "Turn on"}</button>
              </form>
            </td>
          </tr>
          {Object.entries(EDITABLES_CHAT).map(fila)}
        </tbody>
      </table>

      <h2>HubSpot</h2>
      <p>
        People you have marked as <b>contacted</b> (or as clients) are written to
        Handoff&apos;s own HubSpot: the company, the contact, and a note with the fit
        score, why it scored that way, what they asked for and a link back to their
        page here. The evidence itself stays in this panel &mdash; nothing from direct
        messages, no quote from the community, and no channel ever reaches the CRM.
      </p>
      <p>
        {tokenHubspot?.configurado
          ? `Key configured · ends in ${tokenHubspot.ultimos4} · updated ${fecha(tokenHubspot.actualizado)}`
          : "No key yet — nothing is written to the CRM until one is saved."}
      </p>
      <TokenHubspotForm configurado={Boolean(tokenHubspot?.configurado)} />
      <table>
        <tbody>
          <tr>
            <td>Send contacted leads to HubSpot. Turning this on only affects people
              contacted from now on: the backlog is never uploaded on its own, and a
              person is written once and only once.
              <div className="muted" style={{ fontSize: 12 }}>hubspot_activo</div></td>
            <td>
              <form action={guardarHubspotActivo} className="inline">
                <input type="hidden" name="value" value={hubspotActivo ? "false" : "true"} />
                <span>{hubspotActivo ? "On" : "Off"}</span>
                <button type="submit">{hubspotActivo ? "Turn off" : "Turn on"}</button>
              </form>
            </td>
          </tr>
          {Object.entries(EDITABLES_HUBSPOT).map(fila)}
        </tbody>
      </table>
      <p className="muted" style={{ fontSize: 12 }}>
        {hubspotDesde
          ? `Syncing people updated after ${fecha(hubspotDesde)}. Anyone contacted before that is only uploaded by hand.`
          : "Nothing has been synced yet. The first pass marks the moment it starts from, and never looks further back."}
      </p>

      <h2>Target account radar</h2>
      {pausado && (
        <p className="notice error">
          LinkedIn automation is paused — reconnect the account in Unipile, then turn this off.
        </p>
      )}
      <p>
        LinkedIn (Unipile) usage today: <b>{busquedas.count ?? "?"} / {String(valores.get("unipile_busquedas_por_dia") ?? "?")}</b> searches
        {" · "}{perfiles.count ?? "?"} / {String(valores.get("unipile_perfiles_por_dia") ?? "?")} profiles
        <span className="muted"> (day in {zona})</span>
      </p>
      <table>
        <tbody>
          {Object.entries(EDITABLES_RADAR).map(fila)}
          <tr>
            <td>LinkedIn automation paused. The radar turns this on by itself if the account
              gets disconnected or restricted; only a person turns it off.
              <div className="muted" style={{ fontSize: 12 }}>unipile_pausado</div></td>
            <td>
              <form action={guardarPausaUnipile} className="inline">
                <input type="hidden" name="value" value={pausado ? "false" : "true"} />
                <span>{pausado ? "Paused" : "Active"}</span>
                <button type="submit">{pausado ? "Turn off pause" : "Pause now"}</button>
              </form>
            </td>
          </tr>
        </tbody>
      </table>
    </>
  );
}
