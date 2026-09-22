import Link from "next/link";
import { notFound } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { ESTADOS_EDITABLES, fecha, usd } from "@/lib/format";
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
      <p><Link href="/personas">← Personas</Link></p>
      <h1>{p.full_name ?? p.slack_user_id}
        {d.persona?.cargo && <span className="muted"> — {d.persona.cargo}</span>}</h1>

      <div className="two">
        <div className="card">
          {dossier.data ? (
            <>
              <p className={`fit-${d.encaje_handoff?.puntuacion}`}>
                Encaje {d.encaje_handoff?.puntuacion ?? "?"}/3</p>
              <p>{d.encaje_handoff?.razon}</p>
              <h2>Resumen</h2>
              <p>{d.resumen}</p>
              <h2>Empresa</h2>
              <p>
                <b>{d.empresa?.nombre ?? p.company_name ?? "—"}</b>
                {d.empresa?.dominio && <> · {d.empresa.dominio}</>}
                {d.empresa?.sector && <> · {d.empresa.sector}</>}
                {d.empresa?.empleados_aprox && <> · ~{d.empresa.empleados_aprox} personas</>}
                {d.empresa?.ubicacion && <> · {d.empresa.ubicacion}</>}
              </p>
              {d.empresa?.descripcion && <p className="muted">{d.empresa.descripcion}</p>}
              <h2>Contratación</h2>
              <p>
                {d.contratacion?.vacantes_abiertas != null
                  ? `${d.contratacion.vacantes_abiertas} vacantes abiertas` : "Vacantes: sin datos"}
                {d.contratacion?.roles_deslocalizables?.length
                  ? ` · cubribles desde LATAM: ${d.contratacion.roles_deslocalizables.join(", ")}` : ""}
              </p>
              <h2>Señales</h2>
              {d.senales_contexto?.length ? (
                <ul className="plain">
                  {d.senales_contexto.map((s, i) => (
                    <li key={i}>{s.hecho} {s.fuente && <a href={s.fuente} target="_blank" rel="noreferrer">↗</a>}</li>
                  ))}
                </ul>
              ) : <p className="muted">Ninguna.</p>}
              <h2>Huecos</h2>
              {d.huecos?.length ? (
                <ul className="plain">{d.huecos.map((h, i) => <li key={i} className="muted">{h}</li>)}</ul>
              ) : <p className="muted">Ninguno.</p>}
              <h2>Fuentes ({fuentes.length})</h2>
              <ul className="plain">
                {fuentes.map((f, i) => {
                  const url = typeof f === "string" ? f : f.url;
                  const label = typeof f === "string" ? f : `${f.kind ?? ""} · ${f.title || f.url}`;
                  return <li key={i}><a href={url} target="_blank" rel="noreferrer">{label}</a></li>;
                })}
              </ul>
              <p className="muted">Dossier v{dossier.data.version} · {fecha(dossier.data.created_at)}</p>
            </>
          ) : <p className="muted">Todavía no hay dossier.</p>}
        </div>

        <div style={{ display: "grid", gap: 16, alignContent: "start" }}>
          <div className="card">
            <h2 style={{ marginTop: 0 }}>Acciones</h2>
            <form action={cambiarEstado} className="inline">
              <input type="hidden" name="id" value={p.id} />
              <select key={p.state} name="estado" defaultValue={ESTADOS_EDITABLES.includes(p.state) ? p.state : ""}>
                {!ESTADOS_EDITABLES.includes(p.state) && <option value="" disabled>{p.state}</option>}
                {ESTADOS_EDITABLES.map((e) => <option key={e} value={e}>{e}</option>)}
              </select>
              <button type="submit">Guardar</button>
            </form>
            <p />
            <form action={volverAInvestigar}>
              <input type="hidden" name="id" value={p.id} />
              <input type="hidden" name="slack_user_id" value={p.slack_user_id} />
              <button type="submit" disabled={abierta}>
                {abierta ? "Ya está en cola" : "Volver a investigar"}
              </button>
            </form>
          </div>
          <div className="card">
            <h2 style={{ marginTop: 0 }}>Gasto en esta persona</h2>
            <p className="stat"><span className="n">{usd(gasto)}</span></p>
          </div>
          <div className="card">
            <h2 style={{ marginTop: 0 }}>Historial</h2>
            <ul className="plain">
              {(entregas.data ?? []).map((e, i) => (
                <li key={`e${i}`}>Aviso {e.kind} ({e.band}) · <span className="muted">{fecha(e.created_at)}</span></li>
              ))}
              {(cola.data ?? []).map((j, i) => (
                <li key={`j${i}`}>Research {j.reason}: {j.status}
                  {j.last_error && <span className="muted"> — {j.last_error}</span>} ·{" "}
                  <span className="muted">{fecha(j.created_at)}</span></li>
              ))}
              {(acciones.data ?? []).map((a, i) => (
                <li key={`a${i}`}>{a.action} · <span className="muted">{fecha(a.created_at)}</span></li>
              ))}
            </ul>
          </div>
        </div>
      </div>

      {d.datos_proveedor && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Datos de proveedor (sin verificar)</h2>
          <p className="muted">
            Vienen de un proveedor externo, sin fuente citable, y pueden contradecir al resto
            del dossier (por ejemplo, otra sede o otra cifra de empleados).
          </p>
          <p>
            {d.datos_proveedor.empleados_linkedin != null && (
              <>Empleados según LinkedIn: {d.datos_proveedor.empleados_linkedin.toLocaleString("es-CO")}<br /></>
            )}
            {d.datos_proveedor.empleados_crm != null
              && d.datos_proveedor.empleados_crm !== d.datos_proveedor.empleados_linkedin && (
              <>Empleados según el CRM: {d.datos_proveedor.empleados_crm.toLocaleString("es-CO")}<br /></>
            )}
            {d.datos_proveedor.rango_empleados
              && (d.datos_proveedor.rango_empleados.min != null
                || d.datos_proveedor.rango_empleados.max != null) && (
              <>Rango: {d.datos_proveedor.rango_empleados.min ?? "?"}–{d.datos_proveedor.rango_empleados.max ?? "?"}<br /></>
            )}
            {d.datos_proveedor.ingresos_anuales_usd != null && (
              <>Ingresos anuales: {usd(d.datos_proveedor.ingresos_anuales_usd, 0)}<br /></>
            )}
            {d.datos_proveedor.anio_fundacion != null && (
              <>Fundada en {d.datos_proveedor.anio_fundacion}<br /></>
            )}
            {d.datos_proveedor.sede && (
              <>Sede (CRM): {[d.datos_proveedor.sede.ciudad, d.datos_proveedor.sede.region, d.datos_proveedor.sede.pais]
                .filter(Boolean).join(", ")}<br /></>
            )}
            {d.datos_proveedor.ubicacion_linkedin && (
              <>Ubicación según LinkedIn: {[
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
              <>Seguidores en LinkedIn: {d.datos_proveedor.seguidores_linkedin.toLocaleString("es-CO")}<br /></>
            )}
            {d.datos_proveedor.antiguedad_media && (
              <>Antigüedad media del equipo: {d.datos_proveedor.antiguedad_media}<br /></>
            )}
          </p>
          {d.datos_proveedor.empleados_por_area
            && Object.keys(d.datos_proveedor.empleados_por_area).length > 0 && (
            <>
              <h3>Empleados por área</h3>
              <table><tbody>
                {Object.entries(d.datos_proveedor.empleados_por_area)
                  .sort((a, b) => b[1] - a[1])
                  .map(([area, n]) => (
                    <tr key={area}>
                      <td>{area.replace(/_/g, " ")}</td>
                      <td>{n.toLocaleString("es-CO")}</td>
                    </tr>
                  ))}
              </tbody></table>
            </>
          )}
          {d.datos_proveedor.crecimiento?.length ? (
            <>
              <h3>Crecimiento de la plantilla</h3>
              <ul className="plain">
                {d.datos_proveedor.crecimiento.map((c, i) => (
                  <li key={i}>
                    {c.meses} meses: {c.cambio_neto >= 0 ? "+" : ""}{c.cambio_neto}
                    {" "}({(c.porcentaje * 100).toFixed(1)}%)
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          {d.datos_proveedor.consultado && (
            <p className="muted">Consultado el {d.datos_proveedor.consultado}</p>
          )}
        </div>
      )}

      <h2>Mensajes en el Founders Club</h2>
      {(mensajes.data ?? []).length ? (
        <table><tbody>
          {mensajes.data!.map((m) => (
            <tr key={`${m.channel_id}${m.ts}`}>
              <td>{m.text}</td><td className="muted">{fecha(m.created_at)}</td>
            </tr>
          ))}
        </tbody></table>
      ) : <p className="muted">Ninguno leído todavía.</p>}
    </>
  );
}
