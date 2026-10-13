import { createClient } from "@/lib/supabase/server";
import { canalLabel, fecha, minutosDesde } from "@/lib/format";
import { guardarExigirEvidencia, guardarPausaUnipile, guardarUmbral } from "../acciones";
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

// Radar de cuentas objetivo (0012_radar_cuentas.sql): los numéricos que la
// política panel_ajusta_radar deja tocar. unipile_pausado va aparte porque
// es un booleano.
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
    .in("key", [...Object.keys(EDITABLES), ...Object.keys(EDITABLES_RADAR), "unipile_pausado", "unipile_zona", "slack_channel_ids",
      "lector_estado", "exigir_evidencia"]);
  const valores = new Map((data ?? []).map((r) => [r.key, r.value]));
  const canalesValor = valores.get("slack_channel_ids");
  const canales = Array.isArray(canalesValor)
    ? canalesValor.filter((c): c is string => typeof c === "string")
    : [];
  // Los nombres que el worker ya averiguó (conversations.info, solo lectura): un código
  // de canal no le dice nada a nadie.
  const { data: nombresData } = await supabase.from("canales").select("channel_id, nombre");
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
        <p>
          {canales.length > 0
            ? canales.map(canalVisible).join(", ")
            : "No channels configured — the agent is not reading anything"}
        </p>
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
