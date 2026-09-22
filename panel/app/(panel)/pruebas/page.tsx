import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { bandLabel, estadoLabel, jobStatusLabel, usd } from "@/lib/format";
import { refrescarPruebas } from "../acciones";
import { SimularForm } from "./SimularForm";
import { BorrarSimuladosButton } from "./BorrarSimuladosButton";

// La misma columna que usan scripts/simular.py (prefijo "USIM") y
// tarjeta_prueba.py (prefijo "UPRUEBA") para marcar a alguien como simulado.
const PREFIXES = "slack_user_id.like.USIM%,slack_user_id.like.UPRUEBA%";

type Persona = {
  id: string;
  full_name: string | null;
  company_name: string | null;
  slack_user_id: string;
  state: string;
  created_at: string;
  sin_tarjeta: boolean;
};
type Dossier = { prospect_id: string; version: number; content: Record<string, unknown> };
type Job = { slack_user_id: string; status: string; attempts: number };
type Entrega = { prospect_id: string; band: string };
type Gasto = { prospect_id: string | null; cost_usd: number };

// Se queda con la primera fila de cada clave -- las consultas de abajo ya
// vienen ordenadas por lo más reciente primero, así la primera es la última.
function latestByKey<T extends Record<string, unknown>>(rows: T[], key: keyof T): Map<unknown, T> {
  const map = new Map<unknown, T>();
  for (const row of rows) {
    if (!map.has(row[key])) map.set(row[key], row);
  }
  return map;
}

export default async function Pruebas() {
  const supabase = await createClient();
  const { data: personasData, error } = await supabase
    .from("prospects")
    .select("id, full_name, company_name, slack_user_id, state, created_at, sin_tarjeta")
    .or(PREFIXES)
    .order("created_at", { ascending: false })
    .limit(200);
  const personas = (personasData ?? []) as Persona[];

  const ids = personas.map((p) => p.id);
  const slackIds = personas.map((p) => p.slack_user_id);

  const [dossiers, jobs, entregas, llm, otros] = await Promise.all([
    ids.length
      ? supabase.from("dossiers").select("prospect_id, version, content")
          .in("prospect_id", ids).order("version", { ascending: false })
      : { data: [] as Dossier[] },
    slackIds.length
      ? supabase.from("research_jobs").select("slack_user_id, status, attempts")
          .in("slack_user_id", slackIds).order("created_at", { ascending: false })
      : { data: [] as Job[] },
    ids.length
      ? supabase.from("deliveries").select("prospect_id, band")
          .eq("kind", "slack").in("prospect_id", ids).order("created_at", { ascending: false })
      : { data: [] as Entrega[] },
    ids.length
      ? supabase.from("llm_calls").select("prospect_id, cost_usd").in("prospect_id", ids)
      : { data: [] as Gasto[] },
    ids.length
      ? supabase.from("cost_events").select("prospect_id, cost_usd").in("prospect_id", ids)
      : { data: [] as Gasto[] },
  ]);

  const dossierPorPersona = latestByKey((dossiers.data ?? []) as Dossier[], "prospect_id");
  const jobPorPersona = latestByKey((jobs.data ?? []) as Job[], "slack_user_id");
  const entregaPorPersona = latestByKey((entregas.data ?? []) as Entrega[], "prospect_id");

  const gastoPorPersona = new Map<string, number>();
  for (const fila of [...((llm.data ?? []) as Gasto[]), ...((otros.data ?? []) as Gasto[])]) {
    if (!fila.prospect_id) continue;
    gastoPorPersona.set(fila.prospect_id, (gastoPorPersona.get(fila.prospect_id) ?? 0) + Number(fila.cost_usd));
  }

  return (
    <>
      <h1>Test mode</h1>
      <p className="notice">
        This writes to the real database, spends real money on research (OpenAI, Serper), and
        posts a real card in Slack (and an SMS, if that&apos;s configured) -- exactly like a real
        message in the Founders Club would.
      </p>

      <div className="two">
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Create a test person</h2>
          <SimularForm />
        </div>

        <div style={{ display: "grid", gap: 16, alignContent: "start" }}>
          <div className="card">
            <h2 style={{ marginTop: 0 }}>Cleanup</h2>
            <p className="muted">
              Removes every test person (and their messages) created here or from the terminal
              script. Nothing else is touched.
            </p>
            <BorrarSimuladosButton />
          </div>
          <div className="card">
            <form action={refrescarPruebas}>
              <button type="submit">Refresh</button>
            </form>
          </div>
        </div>
      </div>

      <h2>Test people ({personas.length})</h2>
      {error && <p className="notice">Couldn&apos;t load test people: {error.message}</p>}
      <table>
        <thead>
          <tr>
            <th>Person</th>
            <th>Company</th>
            <th>Status</th>
            <th>Research job</th>
            <th>Dossier</th>
            <th>Slack card</th>
            <th>Spend</th>
          </tr>
        </thead>
        <tbody>
          {personas.map((p) => {
            const dossier = dossierPorPersona.get(p.id);
            const fit = dossier
              ? ((dossier.content as { encaje_handoff?: { puntuacion?: number } })
                  .encaje_handoff?.puntuacion ?? null)
              : null;
            const job = jobPorPersona.get(p.slack_user_id);
            const entrega = entregaPorPersona.get(p.id);
            return (
              <tr key={p.id}>
                <td><Link href={`/personas/${p.id}`}>{p.full_name ?? p.slack_user_id}</Link></td>
                <td>{p.company_name ?? <span className="muted">—</span>}</td>
                <td><span className={`pill ${p.state}`}>{estadoLabel(p.state)}</span></td>
                <td>
                  {job
                    ? `${jobStatusLabel(job.status)} (${job.attempts} attempt${job.attempts === 1 ? "" : "s"})`
                    : <span className="muted">None queued</span>}
                </td>
                <td className={fit ? `fit-${fit}` : "muted"}>
                  {dossier ? `v${dossier.version} · fit ${fit ?? "?"}/3` : "No dossier yet"}
                </td>
                <td>
                  {entrega ? (
                    bandLabel(entrega.band)
                  ) : p.sin_tarjeta ? (
                    <span className="muted">No card (test)</span>
                  ) : (
                    <span className="muted">No</span>
                  )}
                </td>
                <td>{usd(gastoPorPersona.get(p.id) ?? 0)}</td>
              </tr>
            );
          })}
          {personas.length === 0 && (
            <tr><td colSpan={7} className="muted">No test people yet.</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}
