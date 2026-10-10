import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { ESTADOS, empresaVisible, estadoLabel, fecha } from "@/lib/format";

type Fila = {
  id: string; full_name: string | null; company_name: string | null; slack_user_id: string;
  state: string; updated_at: string;
  dossiers: { version: number; puntuacion: string | null; created_at: string }[];
};

// Sin ningún parámetro la lista enseña lo que importa: encaje ≥ 2 y sin
// descartados. "todos=1" la abre entera. Cualquier filtro explícito manda y
// funciona como siempre. "activos" es un valor solo de la URL (no un estado de
// la base): todos menos 'descartado'.
const ACTIVOS = "activos";

export default async function Personas(props: PageProps<"/personas">) {
  const params = (await props.searchParams) as Record<string, string | undefined>;
  const porDefecto = !params.estado && !params.encaje && !params.todos;
  const estado = porDefecto ? ACTIVOS : params.estado;
  const encaje = porDefecto ? "2" : params.encaje;

  const supabase = await createClient();
  let query = supabase
    .from("prospects")
    .select("id, full_name, company_name, slack_user_id, state, updated_at, "
      + "dossiers(version, created_at, puntuacion:content->encaje_handoff->>puntuacion)")
    .order("updated_at", { ascending: false })
    .order("version", { referencedTable: "dossiers", ascending: false })
    .limit(1, { referencedTable: "dossiers" })
    .limit(300);
  if (estado === ACTIVOS) query = query.neq("state", "descartado");
  else if (estado) query = query.eq("state", estado);
  const { data, error } = await query;
  const filas = ((data ?? []) as unknown as Fila[]).filter((p) =>
    encaje ? Number(p.dossiers[0]?.puntuacion ?? -1) >= Number(encaje) : true);

  // Cada chip es un interruptor: pulsar el activo lo quita. Sin filtros queda
  // "todos=1", porque una URL sin nada volvería a la vista por defecto.
  const link = (cambio: { estado?: string | null; encaje?: string | null }) => {
    const siguiente = { estado, encaje, ...cambio };
    const q = new URLSearchParams();
    if (siguiente.estado) q.set("estado", siguiente.estado);
    if (siguiente.encaje) q.set("encaje", siguiente.encaje);
    if (q.size === 0) q.set("todos", "1");
    return `/personas?${q}`;
  };
  const chipEstado = (valor: string, texto: string) => (
    <a key={valor} href={link({ estado: estado === valor ? null : valor })}
      className={estado === valor ? "on" : ""}>{texto}</a>
  );

  return (
    <>
      <h1>People <span className="muted">({filas.length})</span></h1>
      <p className="muted">
        {porDefecto
          ? "Showing people with fit 2 or higher, without the discarded ones. "
            + "Tap All people to see everyone."
          : "Everyone the agent has looked at. Tap a person to open their dossier."}
      </p>
      <div className="filters scroll">
        <a href={link({ estado: null, encaje: null })}
          className={!estado && !encaje ? "on all" : "all"}>All people</a>
        {chipEstado(ACTIVOS, "Not discarded")}
        {["3", "2", "1"].map((n) => (
          <a key={n} href={link({ encaje: encaje === n ? null : n })}
            className={encaje === n ? "on" : ""}>Fit ≥ {n}</a>
        ))}
        {ESTADOS.map((e) => chipEstado(e, estadoLabel(e)))}
      </div>
      {error && <p className="notice">Couldn&apos;t load people: {error.message}</p>}

      <table className="only-wide">
        <thead>
          <tr><th>Person</th><th>Company</th><th>Status</th><th>Fit</th><th>Last updated</th></tr>
        </thead>
        <tbody>
          {filas.map((p) => {
            const fit = p.dossiers[0]?.puntuacion;
            return (
              <tr key={p.id}>
                <td><Link href={`/personas/${p.id}`}>{p.full_name ?? p.slack_user_id}</Link></td>
                <td>{empresaVisible(p.company_name) ?? <span className="muted">—</span>}</td>
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

      <div className="cards only-narrow">
        {filas.map((p) => {
          const fit = p.dossiers[0]?.puntuacion;
          return (
            <Link key={p.id} href={`/personas/${p.id}`} className="card person tap">
              <div className="head">
                <span className="name">{p.full_name ?? p.slack_user_id}</span>
                <span className={`pill ${p.state}`}>{estadoLabel(p.state)}</span>
              </div>
              <div className="muted">
                {empresaVisible(p.company_name) ?? "—"} ·{" "}
                <span className={fit ? `fit-${fit}` : ""}>
                  {fit ? `Fit ${fit}/3` : "no dossier"}
                </span>
              </div>
            </Link>
          );
        })}
        {filas.length === 0 && <p className="card muted">No one matches these filters.</p>}
      </div>
    </>
  );
}
