import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { estadoCuentaLabel, fuenteCuentaLabel, hace } from "@/lib/format";
import { Avisos, Badges } from "../senales/componentes";
import { AgregarCuentaForm } from "./AgregarCuentaForm";

type Senal = {
  id: string; title: string; score: number; status: string; reposted: boolean;
  first_seen_at: string; closed_at: string | null;
};
type Cuenta = {
  id: string; name: string; domain: string | null; source: string; status: string;
  last_scan_at: string | null; last_scan_error: string | null;
  hiring_signals: Senal[];
};

const FILTROS: Record<string, string> = {
  hiring: "Hiring now",
  sin_senales: "No signals",
  pausadas: "Paused",
};

export default async function Cuentas(props: PageProps<"/cuentas">) {
  const { filtro, aviso, error: errorAccion } =
    (await props.searchParams) as Record<string, string | undefined>;
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("target_accounts")
    .select("id, name, domain, source, status, last_scan_at, last_scan_error, "
      + "hiring_signals(id, title, score, status, reposted, first_seen_at, closed_at)")
    .order("name")
    .limit(500);

  const cuentas = ((data ?? []) as unknown as Cuenta[]).map((c) => {
    const abiertas = c.hiring_signals.filter((s) => !s.closed_at);
    const top = [...abiertas].sort((a, b) =>
      b.score - a.score || b.first_seen_at.localeCompare(a.first_seen_at))[0];
    return { ...c, abiertas, nuevas: abiertas.filter((s) => s.status === "new").length, top };
  });
  const filas = cuentas.filter((c) =>
    filtro === "hiring" ? c.abiertas.length > 0
      : filtro === "sin_senales" ? c.abiertas.length === 0
      : filtro === "pausadas" ? c.status === "paused"
      : true);

  const link = (f?: string) => `/cuentas${f ? `?filtro=${f}` : ""}`;

  return (
    <>
      <h1>Accounts <span className="muted">({filas.length})</span></h1>
      <Avisos aviso={aviso} error={errorAccion} />
      <div className="filters">
        <a href={link()} className={!filtro ? "on" : ""}>All</a>
        {Object.entries(FILTROS).map(([f, label]) => (
          <a key={f} href={link(f)} className={filtro === f ? "on" : ""}>{label}</a>
        ))}
      </div>
      {error && <p className="notice">Couldn&apos;t load accounts: {error.message}</p>}
      <table>
        <thead>
          <tr><th>Company</th><th>Domain</th><th>Source</th><th>Open roles</th>
            <th>Top signal</th><th>Last scan</th></tr>
        </thead>
        <tbody>
          {filas.map((c) => (
            <tr key={c.id}>
              <td>
                <Link href={`/cuentas/${c.id}`}>{c.name}</Link>
                {c.status === "paused" && <> <span className="pill paused">{estadoCuentaLabel(c.status)}</span></>}
              </td>
              <td>{c.domain ?? <span className="muted">—</span>}</td>
              <td>{fuenteCuentaLabel(c.source)}</td>
              <td>
                {c.abiertas.length}
                {c.nuevas > 0 && <span className="muted"> ({c.nuevas} new)</span>}
              </td>
              <td>
                {c.top ? (
                  <>{c.top.title} <span className="muted">· {c.top.score}</span><Badges s={c.top} /></>
                ) : <span className="muted">—</span>}
              </td>
              <td>
                {c.last_scan_at ? hace(c.last_scan_at)
                  : <span className="muted">{c.status === "watching" ? "Queued" : "Never"}</span>}
                {c.last_scan_error && (
                  <div className="error" style={{ fontSize: 12 }}>{c.last_scan_error}</div>
                )}
              </td>
            </tr>
          ))}
          {filas.length === 0 && (
            <tr><td colSpan={6} className="muted">No accounts match this filter.</td></tr>
          )}
        </tbody>
      </table>

      <h2>Add account</h2>
      <div className="card" style={{ maxWidth: 480 }}>
        <AgregarCuentaForm />
      </div>
    </>
  );
}
