import Link from "next/link";
import { notFound } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import {
  diasDesde, estadoCuentaLabel, estadoSenalLabel, estadoSenalVisible, fecha,
  fuenteCuentaLabel, fuenteVacanteLabel, hace,
} from "@/lib/format";
import { elegirCandidato, escanearCuenta, estadoCuenta } from "../../acciones";
import { AccionesSenal, Avisos, Badges } from "../../senales/componentes";

type Senal = {
  id: string; title: string; location: string | null; sources: string[]; urls: string[];
  first_seen_at: string; last_seen_at: string; closed_at: string | null; reposted: boolean;
  score: number; status: string; snoozed_until: string | null;
};
type Candidato = {
  id: string; signal_id: string; full_name: string | null; headline: string | null;
  profile_url: string | null; reason: string | null; chosen: boolean; prospect_id: string | null;
  rank: number | null;
};

// Solo enlaces http(s): las URLs de las vacantes vienen de JobSpy.
const enlace = (url?: string) => (url && /^https?:\/\//.test(url) ? url : undefined);

export default async function Cuenta(props: PageProps<"/cuentas/[id]">) {
  const { id } = await props.params;
  const { aviso, error: errorAccion } = (await props.searchParams) as Record<string, string | undefined>;
  const supabase = await createClient();
  const { data: c } = await supabase.from("target_accounts").select("*").eq("id", id).maybeSingle();
  if (!c) notFound();

  const { data: senalesData, error } = await supabase.from("hiring_signals")
    .select("id, title, location, sources, urls, first_seen_at, last_seen_at, closed_at, "
      + "reposted, score, status, snoozed_until")
    .eq("account_id", id)
    .order("score", { ascending: false })
    .order("first_seen_at", { ascending: false });
  const senales = (senalesData ?? []) as unknown as Senal[];
  const abiertas = senales.filter((s) => !s.closed_at);
  const cerradas = senales.filter((s) => s.closed_at);

  const { data: candidatosData } = senales.length
    ? await supabase.from("decision_candidates")
      .select("id, signal_id, full_name, headline, profile_url, reason, chosen, prospect_id, rank")
      .in("signal_id", senales.map((s) => s.id))
      .order("rank", { ascending: true, nullsFirst: false })
    : { data: [] };
  const porSenal = new Map<string, Candidato[]>();
  for (const k of (candidatosData ?? []) as Candidato[]) {
    porSenal.set(k.signal_id, [...(porSenal.get(k.signal_id) ?? []), k]);
  }
  const conCandidatos = senales.filter((s) => porSenal.has(s.id));

  const volver = `/cuentas/${id}`;
  const dominio = c.domain as string | null;
  const linkedin = c.linkedin_company_id as string | null;
  const pausada = c.status === "paused";

  return (
    <>
      <p><Link href="/cuentas">← Accounts</Link></p>
      <h1>{c.name} <span className={`pill ${c.status}`}>{estadoCuentaLabel(c.status)}</span></h1>
      <Avisos aviso={aviso} error={errorAccion} />

      <div className="two">
        <div className="card">
          <p>
            Website: {dominio
              ? <a href={`https://${dominio}`} target="_blank" rel="noreferrer">{dominio}</a>
              : <span className="muted">No domain on file.</span>}
            <br />
            LinkedIn: {linkedin
              ? <a href={`https://www.linkedin.com/company/${linkedin}`} target="_blank" rel="noreferrer">
                  {c.linkedin_name ?? linkedin}</a>
              : <span className="muted">Not resolved yet.</span>}
            {c.careers_url && enlace(c.careers_url) && (
              <><br />Careers: <a href={c.careers_url} target="_blank" rel="noreferrer">{c.careers_url}</a></>
            )}
            {c.hubspot_company_id && <><br />HubSpot company: {c.hubspot_company_id}</>}
            <br />
            Source: {fuenteCuentaLabel(c.source)} · added {fecha(c.created_at)}
          </p>
          <p>
            Last scan: {c.last_scan_at
              ? <>{hace(c.last_scan_at)} <span className="muted">({fecha(c.last_scan_at)})</span></>
              : <span className="muted">{pausada ? "Never" : "Queued for the next cycle"}</span>}
          </p>
          {c.last_scan_error && <p className="notice error">Last scan failed: {c.last_scan_error}</p>}
        </div>
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Actions</h2>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <form action={escanearCuenta} className="inline">
              <input type="hidden" name="id" value={c.id} />
              <input type="hidden" name="volver" value={volver} />
              <button type="submit" className="primary" disabled={pausada}>Scan now</button>
            </form>
            <form action={estadoCuenta} className="inline">
              <input type="hidden" name="id" value={c.id} />
              <input type="hidden" name="estado" value={pausada ? "watching" : "paused"} />
              <input type="hidden" name="volver" value={volver} />
              <button type="submit">{pausada ? "Resume" : "Pause"}</button>
            </form>
          </div>
          {pausada && <p className="muted">Paused accounts aren&apos;t scanned. Resume to scan again.</p>}
        </div>
      </div>

      {error && <p className="notice">Couldn&apos;t load roles: {error.message}</p>}

      <h2>Open roles ({abiertas.length})</h2>
      <TablaVacantes senales={abiertas} volver={volver} />

      {conCandidatos.map((s) => (
        <div key={s.id}>
          <h2>Decision makers · {s.title}{s.closed_at && <span className="muted"> (closed)</span>}</h2>
          <table>
            <thead><tr><th>Person</th><th>Why</th><th /></tr></thead>
            <tbody>
              {porSenal.get(s.id)!.map((k) => (
                <tr key={k.id}>
                  <td>
                    {enlace(k.profile_url ?? undefined)
                      ? <a href={k.profile_url!} target="_blank" rel="noreferrer">{k.full_name ?? "Unnamed"}</a>
                      : k.full_name ?? <span className="muted">Unnamed</span>}
                    {k.chosen && <> <span className="pill ready">Chosen</span></>}
                    {k.headline && <div className="muted" style={{ fontSize: 13 }}>{k.headline}</div>}
                  </td>
                  <td>{k.reason ?? <span className="muted">—</span>}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {!k.chosen && (
                      <form action={elegirCandidato} className="inline">
                        <input type="hidden" name="id" value={k.id} />
                        <input type="hidden" name="volver" value={volver} />
                        <button type="submit">Choose</button>
                      </form>
                    )}
                    {k.prospect_id && <> <Link href={`/personas/${k.prospect_id}`}>Dossier</Link></>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      <h2>Closed roles</h2>
      {cerradas.length ? (
        <details>
          <summary>{cerradas.length} closed {cerradas.length === 1 ? "role" : "roles"}</summary>
          <p />
          <TablaVacantes senales={cerradas} volver={volver} cerradas />
        </details>
      ) : <p className="muted">None.</p>}
    </>
  );
}

function TablaVacantes({ senales, volver, cerradas = false }: {
  senales: Senal[]; volver: string; cerradas?: boolean;
}) {
  return (
    <table>
      <thead>
        <tr><th>Role</th><th>Location</th><th>Seen on</th><th>{cerradas ? "Closed" : "Open"}</th>
          <th>Score</th><th>Status</th>{!cerradas && <th />}</tr>
      </thead>
      <tbody>
        {senales.map((s) => {
          const estado = estadoSenalVisible(s);
          const url = enlace(s.urls[0]);
          return (
            <tr key={s.id}>
              <td>
                {url ? <a href={url} target="_blank" rel="noreferrer">{s.title}</a> : s.title}
                {!cerradas && <Badges s={s} />}
              </td>
              <td>{s.location ?? <span className="muted">—</span>}</td>
              <td>{s.sources.length ? s.sources.map(fuenteVacanteLabel).join(", ")
                : <span className="muted">—</span>}</td>
              <td>{cerradas ? hace(s.closed_at) : `${diasDesde(s.first_seen_at)}d`}</td>
              <td>{s.score}</td>
              <td><span className={`pill ${estado}`}>{estadoSenalLabel(estado)}</span></td>
              {!cerradas && <td><AccionesSenal s={s} volver={volver} /></td>}
            </tr>
          );
        })}
        {senales.length === 0 && (
          <tr><td colSpan={cerradas ? 6 : 7} className="muted">
            No roles here yet.</td></tr>
        )}
      </tbody>
    </table>
  );
}
