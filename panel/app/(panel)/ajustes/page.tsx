import { createClient } from "@/lib/supabase/server";
import { canalLabel, fecha, minutosDesde } from "@/lib/format";
import {
  guardarCanalesTodos, guardarCanalPresentaciones, guardarExigirEvidencia,
  guardarDmTextoActivo, guardarLeerHilos, guardarPausaUnipile, guardarSeguimientoActivo, guardarUmbral,
} from "../acciones";
import { CanalesForm } from "./CanalesForm";
import { ConectarClaude, type SesionOauth, type TokenMcp } from "./ConectarClaude";
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

// Radar de cuentas objetivo (0012_radar_cuentas.sql): los numéricos que la
// política panel_ajusta_radar deja tocar. unipile_pausado va aparte porque
// es un booleano.
// Avisos de canal (0030_eventos_canal.sql): no cuestan una llamada al modelo, así que
// sus topes son altos. canal_presentaciones va aparte porque es una cadena.
const EDITABLES_EVENTOS: Record<string, string> = {
  menciones_por_dia: "Maximum \"you were mentioned\" alerts per day",
  presentaciones_por_dia: "Maximum \"new introduction\" alerts per day",
};

// Respuestas de los hilos (0032_hilos.sql): los numéricos que la política
// panel_ajusta_hilos deja tocar. leer_hilos va aparte porque es un booleano.
const EDITABLES_HILOS: Record<string, string> = {
  hilos_dias: "Days back the agent looks for new replies in older threads",
  hilos_por_vuelta: "Maximum threads read in one poll",
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
      ...Object.keys(EDITABLES_HILOS), "leer_hilos",
      "canal_presentaciones", "seguimiento_activo", "dm_texto_activo",
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
  const canalIntros = String(valores.get("canal_presentaciones") ?? "");
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

      <h2>Channel alerts</h2>
      <p>
        Two alerts that cost nothing to work out, so they arrive in the same poll:
        when someone mentions you in a watched channel, and when someone posts in the
        introductions channel. Both are posted in the cards channel.
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
        <tbody>{Object.entries(EDITABLES_EVENTOS).map(fila)}</tbody>
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
