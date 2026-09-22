import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { ESTADOS, estadoLabel, fecha } from "@/lib/format";

type Fila = {
  id: string; full_name: string | null; company_name: string | null; slack_user_id: string;
  state: string; updated_at: string;
  dossiers: { version: number; puntuacion: string | null; created_at: string }[];
};

export default async function Personas(props: PageProps<"/personas">) {
  const { estado, encaje } = (await props.searchParams) as Record<string, string | undefined>;
  const supabase = await createClient();
  let query = supabase
    .from("prospects")
    .select("id, full_name, company_name, slack_user_id, state, updated_at, "
      + "dossiers(version, created_at, puntuacion:content->encaje_handoff->>puntuacion)")
    .order("updated_at", { ascending: false })
    .order("version", { referencedTable: "dossiers", ascending: false })
    .limit(1, { referencedTable: "dossiers" })
    .limit(300);
  if (estado) query = query.eq("state", estado);
  const { data, error } = await query;
  const filas = ((data ?? []) as unknown as Fila[]).filter((p) =>
    encaje ? Number(p.dossiers[0]?.puntuacion ?? -1) >= Number(encaje) : true);

  const link = (params: Record<string, string | undefined>) => {
    const q = new URLSearchParams(Object.entries({ estado, encaje, ...params })
      .filter(([, v]) => v) as [string, string][]);
    return `/personas${q.size ? `?${q}` : ""}`;
  };

  return (
    <>
      <h1>People <span className="muted">({filas.length})</span></h1>
      <div className="filters">
        <a href={link({ estado: undefined })} className={!estado ? "on" : ""}>All</a>
        {ESTADOS.map((e) => (
          <a key={e} href={link({ estado: e })} className={estado === e ? "on" : ""}>{estadoLabel(e)}</a>
        ))}
      </div>
      <div className="filters">
        <a href={link({ encaje: undefined })} className={!encaje ? "on" : ""}>Any fit</a>
        {["3", "2", "1"].map((n) => (
          <a key={n} href={link({ encaje: n })} className={encaje === n ? "on" : ""}>Fit ≥ {n}</a>
        ))}
      </div>
      {error && <p className="notice">Couldn&apos;t load people: {error.message}</p>}
      <table>
        <thead>
          <tr><th>Person</th><th>Company</th><th>Status</th><th>Fit</th><th>Last updated</th></tr>
        </thead>
        <tbody>
          {filas.map((p) => {
            const fit = p.dossiers[0]?.puntuacion;
            return (
              <tr key={p.id}>
                <td><Link href={`/personas/${p.id}`}>{p.full_name ?? p.slack_user_id}</Link></td>
                <td>{p.company_name ?? <span className="muted">—</span>}</td>
                <td><span className={`pill ${p.state}`}>{estadoLabel(p.state)}</span></td>
                <td className={fit ? `fit-${fit}` : "muted"}>{fit ? `${fit}/3` : "no dossier"}</td>
                <td className="muted">{fecha(p.updated_at)}</td>
              </tr>
            );
          })}
          {filas.length === 0 && (
            <tr><td colSpan={5} className="muted">No one matches these filters.</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}
