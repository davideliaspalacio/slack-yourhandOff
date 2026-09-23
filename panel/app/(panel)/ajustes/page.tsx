import { createClient } from "@/lib/supabase/server";
import { guardarPausaUnipile, guardarUmbral } from "../acciones";

const EDITABLES: Record<string, string> = {
  banda_alta_min: "Minimum fit score for the high band (card + SMS)",
  banda_media_min: "Minimum fit score for the medium band (card)",
  banda_baja_min: "Minimum fit score for the low band (daily email). Raise it to silence.",
  research_por_hora: "Maximum research jobs per hour",
  sms_por_dia: "Maximum SMS per day",
  sms_hora_inicio: "Hour from which SMS are allowed",
  sms_hora_fin: "Hour until which SMS are allowed",
};

// Radar de cuentas objetivo (0012_radar_cuentas.sql): los numéricos que la
// política panel_ajusta_radar deja tocar. unipile_pausado va aparte porque
// es un booleano.
const EDITABLES_RADAR: Record<string, string> = {
  radar_auto_min: "Minimum score for a role to be pursued automatically (0–10)",
  radar_horas_entre_escaneos: "Hours between scans of the same account",
  unipile_busquedas_por_dia: "Maximum LinkedIn (Unipile) searches per day",
  unipile_perfiles_por_dia: "Maximum LinkedIn (Unipile) profile lookups per day",
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
    .in("key", [...Object.keys(EDITABLES), ...Object.keys(EDITABLES_RADAR), "unipile_pausado", "unipile_zona"]);
  const valores = new Map((data ?? []).map((r) => [r.key, r.value]));
  const pausado = valores.get("unipile_pausado") === true;
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

  return (
    <>
      <h1>Settings</h1>
      <p className="notice">The emergency shutdown and the money caps aren&apos;t changed from here,
        on purpose: they&apos;re edited by hand in the database.</p>
      <table>
        <tbody>
          {Object.entries(EDITABLES).map(fila)}
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
