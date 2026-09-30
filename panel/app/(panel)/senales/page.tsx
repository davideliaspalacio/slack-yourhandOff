import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { estadoSenalLabel, estadoSenalVisible, fecha } from "@/lib/format";
import { AccionesSenal, Avisos, Badges } from "./componentes";

type Senal = {
  id: string; title: string; score: number; status: string; snoozed_until: string | null;
  reposted: boolean; sources: string[]; first_seen_at: string;
  cuenta: { id: string; name: string } | null;
};

// La bandeja: vacantes abiertas que todavía piden algo. Las descartadas, las
// que ya son tarea y las dormidas con el snooze en curso no entran.
const ESTADOS_BANDEJA = ["new", "pursued", "snoozed", "researching", "ready"];

// Cada filtro, por el estado visible (un snooze vencido cuenta como 'new').
const FILTROS: Record<string, { label: string; estados: string[] }> = {
  nuevas: { label: "New", estados: ["new"] },
  en_curso: { label: "In progress", estados: ["pursued", "researching"] },
  listas: { label: "Ready", estados: ["ready"] },
  todas: { label: "All", estados: ["new", "pursued", "researching", "ready"] },
};

export default async function Senales(props: PageProps<"/senales">) {
  const { filtro: filtroParam, aviso, error: errorAccion } =
    (await props.searchParams) as Record<string, string | undefined>;
  const filtro = filtroParam && FILTROS[filtroParam] ? filtroParam : "nuevas";
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("hiring_signals")
    .select("id, title, score, status, snoozed_until, reposted, sources, first_seen_at, "
      + "cuenta:target_accounts(id, name)")
    .is("closed_at", null)
    .in("status", ESTADOS_BANDEJA)
    .order("score", { ascending: false })
    .order("first_seen_at", { ascending: false })
    .limit(500);

  const filas = ((data ?? []) as unknown as Senal[])
    .map((s) => ({ ...s, visible: estadoSenalVisible(s) }))
    .filter((s) => FILTROS[filtro].estados.includes(s.visible));
  const volver = `/senales?filtro=${filtro}`;

  return (
    <>
      <h1>Signals <span className="muted">({filas.length})</span></h1>
      <Avisos aviso={aviso} error={errorAccion} />
      <div className="filters">
        {Object.entries(FILTROS).map(([f, { label }]) => (
          <a key={f} href={`/senales?filtro=${f}`} className={filtro === f ? "on" : ""}>{label}</a>
        ))}
      </div>
      {error && <p className="notice">Couldn&apos;t load signals: {error.message}</p>}
      <table>
        <thead>
          <tr><th>Company</th><th>Role</th><th>Score</th><th>Signals</th><th>First seen</th>
            <th>Status</th><th /></tr>
        </thead>
        <tbody>
          {filas.map((s) => (
            <tr key={s.id}>
              <td>{s.cuenta
                ? <Link href={`/cuentas/${s.cuenta.id}`}>{s.cuenta.name}</Link>
                : <span className="muted">—</span>}</td>
              <td>{s.title}</td>
              <td>{s.score}</td>
              <td><Badges s={s} fuentes /></td>
              <td className="muted">{fecha(s.first_seen_at)}</td>
              <td><span className={`pill ${s.visible}`}>{estadoSenalLabel(s.visible)}</span></td>
              <td><AccionesSenal s={s} volver={volver} /></td>
            </tr>
          ))}
          {filas.length === 0 && (
            <tr><td colSpan={7} className="muted">No signals here.</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}
