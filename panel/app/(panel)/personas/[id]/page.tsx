import Link from "next/link";
import { notFound } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { ESTADOS_EDITABLES, accionLabel, bandLabel, estadoLabel, fecha, jobStatusLabel, reasonLabel, usd } from "@/lib/format";
import { cambiarEstado, volverAInvestigar } from "../../acciones";

type Senal = { hecho?: string; fuente?: string };
type Lugar = { ciudad?: string; region?: string; pais?: string };
// Lo que guarda handoff_agent.tools.enrichment.normalise: datos de un
// proveedor externo, sin fuente citable y a veces contradictorios entre sí.
type DatosProveedor = {
  empleados_linkedin?: number;
  empleados_crm?: number;
  rango_empleados?: { min?: number; max?: number };
  empleados_por_area?: Record<string, number>;
  evolucion_mensual?: { mes: string; empleados: number }[];
  // porcentaje ya viene en puntos porcentuales (0.14 = 0,14 %).
  crecimiento?: { meses: number; cambio_neto: number; porcentaje: number }[];
  ingresos_anuales_usd?: number;
  anio_fundacion?: number;
  sede?: Lugar;
  ubicacion_linkedin?: Lugar;
  linkedin_url?: string;
  seguidores_linkedin?: number;
  antiguedad_media?: string;
  consultado?: string;
};
// Crecimiento a 12 meses calculado de la serie mensual: es el dato coherente
// con el recuento de empleados; el `crecimiento` del proveedor usa otra base.
function crecimientoSerie(serie?: { mes: string; empleados: number }[]) {
  if (!serie || serie.length < 13) return null;
  const antes = serie[serie.length - 13];
  const ahora = serie[serie.length - 1];
  if (!antes.empleados) return null;
  const pct = ((ahora.empleados - antes.empleados) * 100) / antes.empleados;
  return { antes, ahora, pct };
}

type Dossier = {
  persona?: { nombre?: string; cargo?: string; fuente?: string };
  empresa?: { nombre?: string; dominio?: string; sector?: string; empleados_aprox?: number;
              ubicacion?: string; descripcion?: string; fuentes?: string[] };
  contratacion?: { vacantes_abiertas?: number; roles?: string[]; roles_deslocalizables?: string[] };
  senales_contexto?: Senal[];
  encaje_handoff?: { puntuacion?: number; razon?: string };
  huecos?: string[];
  resumen?: string;
  datos_proveedor?: DatosProveedor;
};
// Las 6 filas de la primera prueba guardan URLs sueltas; las demás, objetos.
type Fuente = string | { url: string; kind?: string; title?: string };

export default async function Persona(props: PageProps<"/personas/[id]">) {
  const { id } = await props.params;
  const supabase = await createClient();
  const { data: p } = await supabase.from("prospects").select("*").eq("id", id).maybeSingle();
  if (!p) notFound();

  const [dossier, mensajes, entregas, acciones, cola, llm, otros] = await Promise.all([
    supabase.from("dossiers").select("version, content, sources, created_at")
      .eq("prospect_id", id).order("version", { ascending: false }).limit(1).maybeSingle(),
    supabase.from("slack_messages").select("channel_id, ts, text, status, created_at")
      .eq("user_id", p.slack_user_id).order("created_at", { ascending: false }).limit(20),
    supabase.from("deliveries").select("kind, band, created_at")
      .eq("prospect_id", id).order("created_at", { ascending: false }),
    supabase.from("agent_actions").select("action, created_at")
      .eq("prospect_id", id).order("created_at", { ascending: false }).limit(30),
    supabase.from("research_jobs").select("reason, status, attempts, last_error, created_at")
      .eq("slack_user_id", p.slack_user_id).order("created_at", { ascending: false }).limit(10),
    supabase.from("llm_calls").select("cost_usd").eq("prospect_id", id),
    supabase.from("cost_events").select("cost_usd").eq("prospect_id", id),
  ]);

  const d = (dossier.data?.content ?? {}) as Dossier;
  const fuentes = (dossier.data?.sources ?? []) as Fuente[];
  const gasto = [...(llm.data ?? []), ...(otros.data ?? [])]
    .reduce((s, r) => s + Number(r.cost_usd), 0);
  const abierta = (cola.data ?? []).some((j) => j.status === "pendiente" || j.status === "en_curso");

  return (
    <>
      <p><Link href="/personas">← People</Link></p>
      <h1>{p.full_name ?? p.slack_user_id}
        {d.persona?.cargo && <span className="muted"> — {d.persona.cargo}</span>}</h1>

      <div className="two">
        <div className="card">
          {dossier.data ? (
            <>
              <p className={`fit-${d.encaje_handoff?.puntuacion}`}>
                Fit {d.encaje_handoff?.puntuacion ?? "?"}/3</p>
              <p>{d.encaje_handoff?.razon}</p>
              <h2>Summary</h2>
              <p>{d.resumen}</p>
              <h2>Company</h2>
              <p>
                <b>{d.empresa?.nombre ?? p.company_name ?? "—"}</b>
                {d.empresa?.dominio && <> · {d.empresa.dominio}</>}
                {d.empresa?.sector && <> · {d.empresa.sector}</>}
                {d.empresa?.empleados_aprox && <> · ~{d.empresa.empleados_aprox} people</>}
                {d.empresa?.ubicacion && <> · {d.empresa.ubicacion}</>}
              </p>
              {d.empresa?.descripcion && <p className="muted">{d.empresa.descripcion}</p>}
              <h2>Hiring</h2>
              <p>
                {d.contratacion?.vacantes_abiertas != null
                  ? `${d.contratacion.vacantes_abiertas} open roles` : "Openings: no data"}
                {d.contratacion?.roles_deslocalizables?.length
                  ? ` · coverable from LATAM: ${d.contratacion.roles_deslocalizables.join(", ")}` : ""}
              </p>
              <h2>Context signals</h2>
              {d.senales_contexto?.length ? (
                <ul className="plain">
                  {d.senales_contexto.map((s, i) => (
                    <li key={i}>{s.hecho} {s.fuente && <a href={s.fuente} target="_blank" rel="noreferrer">↗</a>}</li>
                  ))}
                </ul>
              ) : <p className="muted">None.</p>}
              <h2>Gaps</h2>
              {d.huecos?.length ? (
                <ul className="plain">{d.huecos.map((h, i) => <li key={i} className="muted">{h}</li>)}</ul>
              ) : <p className="muted">None.</p>}
              <h2>Sources ({fuentes.length})</h2>
              <ul className="plain">
                {fuentes.map((f, i) => {
                  const url = typeof f === "string" ? f : f.url;
                  const label = typeof f === "string" ? f : `${f.kind ?? ""} · ${f.title || f.url}`;
                  return <li key={i}><a href={url} target="_blank" rel="noreferrer">{label}</a></li>;
                })}
              </ul>
              <p className="muted">Dossier v{dossier.data.version} · {fecha(dossier.data.created_at)}</p>
            </>
          ) : <p className="muted">No dossier yet.</p>}
        </div>

        <div style={{ display: "grid", gap: 16, alignContent: "start" }}>
          <div className="card">
            <h2 style={{ marginTop: 0 }}>Actions</h2>
            <form action={cambiarEstado} className="inline">
              <input type="hidden" name="id" value={p.id} />
              <select key={p.state} name="estado" defaultValue={ESTADOS_EDITABLES.includes(p.state) ? p.state : ""}>
                {!ESTADOS_EDITABLES.includes(p.state) && <option value="" disabled>{estadoLabel(p.state)}</option>}
                {ESTADOS_EDITABLES.map((e) => <option key={e} value={e}>{estadoLabel(e)}</option>)}
              </select>
              <button type="submit">Save</button>
            </form>
            <p />
            <form action={volverAInvestigar}>
              <input type="hidden" name="id" value={p.id} />
              <input type="hidden" name="slack_user_id" value={p.slack_user_id} />
              <button type="submit" disabled={abierta}>
                {abierta ? "Already queued" : "Research again"}
              </button>
            </form>
          </div>
          <div className="card">
            <h2 style={{ marginTop: 0 }}>Spend on this person</h2>
            <p className="stat"><span className="n">{usd(gasto)}</span></p>
          </div>
          <div className="card">
            <h2 style={{ marginTop: 0 }}>History</h2>
            <ul className="plain">
              {(entregas.data ?? []).map((e, i) => (
                <li key={`e${i}`}>Notice {e.kind} ({bandLabel(e.band)}) · <span className="muted">{fecha(e.created_at)}</span></li>
              ))}
              {(cola.data ?? []).map((j, i) => (
                <li key={`j${i}`}>Research {reasonLabel(j.reason)}: {jobStatusLabel(j.status)}
                  {j.last_error && <span className="muted"> — {j.last_error}</span>} ·{" "}
                  <span className="muted">{fecha(j.created_at)}</span></li>
              ))}
              {(acciones.data ?? []).map((a, i) => (
                <li key={`a${i}`}>{accionLabel(a.action)} · <span className="muted">{fecha(a.created_at)}</span></li>
              ))}
            </ul>
          </div>
        </div>
      </div>

      {d.datos_proveedor && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Provider data (unverified)</h2>
          <p className="muted">
            This comes from an external provider, without a citable source, and can contradict
            the rest of the dossier (for example, a different headquarters or employee count).
          </p>
          <p>
            {d.datos_proveedor.empleados_linkedin != null && (
              <>Employees per LinkedIn: {d.datos_proveedor.empleados_linkedin.toLocaleString("en-US")}<br /></>
            )}
            {d.datos_proveedor.empleados_crm != null
              && d.datos_proveedor.empleados_crm !== d.datos_proveedor.empleados_linkedin && (
              <>Employees per the CRM: {d.datos_proveedor.empleados_crm.toLocaleString("en-US")}<br /></>
            )}
            {d.datos_proveedor.rango_empleados
              && (d.datos_proveedor.rango_empleados.min != null
                || d.datos_proveedor.rango_empleados.max != null) && (
              <>Range: {d.datos_proveedor.rango_empleados.min ?? "?"}–{d.datos_proveedor.rango_empleados.max ?? "?"}<br /></>
            )}
            {d.datos_proveedor.ingresos_anuales_usd != null && (
              <>Annual revenue: {usd(d.datos_proveedor.ingresos_anuales_usd, 0)}<br /></>
            )}
            {d.datos_proveedor.anio_fundacion != null && (
              <>Founded in {d.datos_proveedor.anio_fundacion}<br /></>
            )}
            {d.datos_proveedor.sede && (
              <>Headquarters (CRM): {[d.datos_proveedor.sede.ciudad, d.datos_proveedor.sede.region, d.datos_proveedor.sede.pais]
                .filter(Boolean).join(", ")}<br /></>
            )}
            {d.datos_proveedor.ubicacion_linkedin && (
              <>Location per LinkedIn: {[
                d.datos_proveedor.ubicacion_linkedin.ciudad,
                d.datos_proveedor.ubicacion_linkedin.region,
                d.datos_proveedor.ubicacion_linkedin.pais,
              ].filter(Boolean).join(", ")}<br /></>
            )}
            {d.datos_proveedor.linkedin_url?.startsWith("https://www.linkedin.com/company/") && (
              <>LinkedIn: <a href={d.datos_proveedor.linkedin_url} target="_blank" rel="noreferrer">
                {d.datos_proveedor.linkedin_url}</a><br /></>
            )}
            {d.datos_proveedor.seguidores_linkedin != null && (
              <>LinkedIn followers: {d.datos_proveedor.seguidores_linkedin.toLocaleString("en-US")}<br /></>
            )}
            {d.datos_proveedor.antiguedad_media && (
              <>Average team tenure: {d.datos_proveedor.antiguedad_media}<br /></>
            )}
          </p>
          {d.datos_proveedor.empleados_por_area
            && Object.keys(d.datos_proveedor.empleados_por_area).length > 0 && (
            <>
              <h3>Employees by department</h3>
              <table><tbody>
                {Object.entries(d.datos_proveedor.empleados_por_area)
                  .sort((a, b) => b[1] - a[1])
                  .map(([area, n]) => (
                    <tr key={area}>
                      <td>{area.replace(/_/g, " ")}</td>
                      <td>{n.toLocaleString("en-US")}</td>
                    </tr>
                  ))}
              </tbody></table>
            </>
          )}
          {(() => {
            const serie = crecimientoSerie(d.datos_proveedor.evolucion_mensual);
            return serie ? (
              <p>
                Headcount over 12 months: {serie.antes.empleados.toLocaleString("en-US")} ({serie.antes.mes})
                {" → "}{serie.ahora.empleados.toLocaleString("en-US")} ({serie.ahora.mes}),{" "}
                {serie.pct >= 0 ? "+" : ""}{serie.pct.toFixed(0)}%
              </p>
            ) : null;
          })()}
          {d.datos_proveedor.crecimiento?.length ? (
            <>
              <h3>Headcount growth</h3>
              <ul className="plain">
                {d.datos_proveedor.crecimiento.map((c, i) => (
                  <li key={i}>
                    {c.meses} months: {c.cambio_neto >= 0 ? "+" : ""}{c.cambio_neto}
                    {" "}({c.porcentaje.toFixed(2)}% per provider)
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          {d.datos_proveedor.consultado && (
            <p className="muted">Retrieved on {d.datos_proveedor.consultado}</p>
          )}
        </div>
      )}

      <h2>Messages in the Founders Club</h2>
      {(mensajes.data ?? []).length ? (
        <table><tbody>
          {mensajes.data!.map((m) => (
            <tr key={`${m.channel_id}${m.ts}`}>
              <td>{m.text}</td><td className="muted">{fecha(m.created_at)}</td>
            </tr>
          ))}
        </tbody></table>
      ) : <p className="muted">None read yet.</p>}
    </>
  );
}
