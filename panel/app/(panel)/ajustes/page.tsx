import { createClient } from "@/lib/supabase/server";
import { fecha } from "@/lib/format";
import { guardarUmbral } from "../acciones";
import { TokenSlackForm } from "./TokenSlackForm";

const EDITABLES: Record<string, string> = {
  banda_alta_min: "Minimum fit score for the high band (card + SMS)",
  banda_media_min: "Minimum fit score for the medium band (card)",
  banda_baja_min: "Minimum fit score for the low band (daily email). Raise it to silence.",
  research_por_hora: "Maximum research jobs per hour",
  sms_por_dia: "Maximum SMS per day",
  sms_hora_inicio: "Hour from which SMS are allowed",
  sms_hora_fin: "Hour until which SMS are allowed",
};

export default async function Ajustes() {
  const supabase = await createClient();
  const { data } = await supabase.from("config").select("key, value, updated_at")
    .in("key", Object.keys(EDITABLES));
  const valores = new Map((data ?? []).map((r) => [r.key, r.value]));

  // panel_estado_token_slack nunca devuelve el token: solo si hay uno y sus 4 últimos.
  const { data: estado } = await supabase.rpc("panel_estado_token_slack");
  const token = Array.isArray(estado) ? estado[0] : estado;

  return (
    <>
      <h1>Settings</h1>
      <section style={{ marginBottom: 24 }}>
        <h2>Founders Club reader token</h2>
        <p>
          {token?.configurado
            ? `Configured · ends in ${token.sufijo} · updated ${fecha(token.actualizado)} ` +
              `by ${token.por ?? "unknown"}`
            : "Not configured — the agent cannot read the Founders Club yet."}
        </p>
        <TokenSlackForm configurado={Boolean(token?.configurado)} />
      </section>
      <p className="notice">The emergency shutdown and the money caps aren&apos;t changed from here,
        on purpose: they&apos;re edited by hand in the database.</p>
      <table>
        <tbody>
          {Object.entries(EDITABLES).map(([key, label]) => (
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
          ))}
        </tbody>
      </table>
    </>
  );
}
