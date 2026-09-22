import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { usd } from "@/lib/format";

export default async function Costes() {
  const supabase = await createClient();
  const inicioMes = new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), 1)).toISOString();

  const [diario, llm, otros, tope, dossiers, gente] = await Promise.all([
    supabase.from("panel_costes_diarios").select("dia, usd, llamadas").limit(30),
    supabase.from("llm_calls").select("prospect_id, cost_usd, created_at"),
    supabase.from("cost_events").select("prospect_id, source, cost_usd, created_at"),
    supabase.from("config").select("value").eq("key", "monthly_budget_usd").maybeSingle(),
    supabase.from("dossiers").select("prospect_id", { count: "exact", head: true }),
    supabase.from("prospects").select("id, full_name, slack_user_id"),
  ]);

  const filas = [...(llm.data ?? []), ...(otros.data ?? [])];
  const total = filas.reduce((s, r) => s + Number(r.cost_usd), 0);
  const mes = filas.filter((r) => r.created_at >= inicioMes).reduce((s, r) => s + Number(r.cost_usd), 0);
  const limite = Number(tope.data?.value ?? 150);
  const nDossiers = dossiers.count ?? 0;
  const serper = (otros.data ?? []).filter((r) => r.source === "serper_search").length;

  const porPersona = new Map<string, number>();
  for (const r of filas) {
    if (r.prospect_id) porPersona.set(r.prospect_id, (porPersona.get(r.prospect_id) ?? 0) + Number(r.cost_usd));
  }
  const nombres = new Map((gente.data ?? []).map((p) => [p.id, p.full_name ?? p.slack_user_id]));
  const ranking = [...porPersona.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20);

  return (
    <>
      <h1>Costs</h1>
      <div className="grid">
        <div className="card stat"><div className="n">{usd(mes, 2)}</div>
          <div className="l">This month, out of a cap of {usd(limite, 0)} ({((mes / limite) * 100).toFixed(1)}%)</div></div>
        <div className="card stat"><div className="n">{usd(total, 2)}</div><div className="l">All-time total</div></div>
        <div className="card stat"><div className="n">{nDossiers ? usd(total / nDossiers) : "—"}</div>
          <div className="l">Average cost per dossier ({nDossiers} dossiers)</div></div>
        <div className="card stat"><div className="n">{serper}</div><div className="l">Serper searches</div></div>
      </div>

      <h2>By day</h2>
      <table>
        <thead><tr><th>Day</th><th>Spend</th><th>Calls and events</th></tr></thead>
        <tbody>
          {(diario.data ?? []).map((d) => (
            <tr key={d.dia}><td>{d.dia}</td><td>{usd(d.usd)}</td><td>{d.llamadas}</td></tr>
          ))}
          {!(diario.data ?? []).length && <tr><td colSpan={3} className="muted">No spending recorded.</td></tr>}
        </tbody>
      </table>

      <h2>By person</h2>
      <table>
        <thead><tr><th>Person</th><th>Spend</th></tr></thead>
        <tbody>
          {ranking.map(([id, coste]) => (
            <tr key={id}><td><Link href={`/personas/${id}`}>{nombres.get(id) ?? id}</Link></td><td>{usd(coste)}</td></tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
