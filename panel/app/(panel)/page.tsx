import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import {
  DIAS_SENAL_RECIENTE, empresaVisible, hace, haceDias, recortar, tipoSenalLabel,
} from "@/lib/format";
import { cambiarEstado } from "./acciones";

// La pantalla de entrada: qué mirar hoy. Las personas con encaje 2 o 3 que el
// agente investigó hace poco y nadie ha tocado todavía.
const DIAS = 14;
const MAXIMO = 25;

type Fila = {
  id: string; full_name: string | null; company_name: string | null; slack_user_id: string;
  dossiers: {
    version: number; puntuacion: string | null; razon: string | null; created_at: string;
  }[];
};

export default async function Hoy() {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("prospects")
    .select("id, full_name, company_name, slack_user_id, "
      + "dossiers(version, created_at, puntuacion:content->encaje_handoff->>puntuacion, "
      + "razon:content->encaje_handoff->>razon)")
    .in("state", ["nuevo", "investigado"])
    .order("updated_at", { ascending: false })
    .order("version", { referencedTable: "dossiers", ascending: false })
    .limit(1, { referencedTable: "dossiers" })
    .limit(300);

  const desde = haceDias(DIAS);
  const candidatos = ((data ?? []) as unknown as Fila[])
    .map((p) => ({ p, d: p.dossiers[0] }))
    .filter(({ d }) => d && Number(d.puntuacion ?? -1) >= 2
      && new Date(d.created_at).getTime() >= desde)
    .sort((a, b) => b.d.created_at.localeCompare(a.d.created_at))
    .slice(0, MAXIMO);

  // Quién de la lista pidió ayuda en Slack esta semana: no es solo un research más.
  // La más reciente por persona (la consulta ya viene ordenada de más nueva a más vieja).
  const pidio = new Map<string, string>();
  if (candidatos.length) {
    const { data: senales } = await supabase.from("message_signals")
      .select("prospect_id, tipo")
      .in("prospect_id", candidatos.map(({ p }) => p.id))
      .eq("detectada", true)
      .gte("created_at", new Date(haceDias(DIAS_SENAL_RECIENTE)).toISOString())
      .order("created_at", { ascending: false });
    for (const s of senales ?? []) {
      if (s.prospect_id && !pidio.has(s.prospect_id)) pidio.set(s.prospect_id, s.tipo ?? "");
    }
  }

  return (
    <>
      <h1>Today</h1>
      <div className="card intro">
        <p>
          Every time someone posts in the Founders Club Slack, the agent researches them and
          lists the ones worth a conversation here.
        </p>
        <p>
          Open a person to read why, then tap <b>Contacted</b> once you&apos;ve reached out,
          or <b>Discard</b> to take them off this list.
        </p>
      </div>

      <h2>Worth a look <span className="muted">({candidatos.length})</span></h2>
      {error && <p className="notice">Couldn&apos;t load people: {error.message}</p>}
      {candidatos.length === 0 && !error && (
        <p className="card muted">
          Nothing new to review. The agent is watching and will post here when something comes up.
        </p>
      )}
      <div className="cards">
        {candidatos.map(({ p, d }) => {
          const empresa = empresaVisible(p.company_name);
          return (
            <div key={p.id} className="card person">
              <div className="head">
                <Link href={`/personas/${p.id}`} className="name">
                  {p.full_name ?? p.slack_user_id}
                </Link>
                <span className={`pill fit-${d.puntuacion}`}>Fit {d.puntuacion}/3</span>
              </div>
              {pidio.has(p.id) && (
                <div>
                  <span className="pill asked">
                    asked: {tipoSenalLabel(pidio.get(p.id) || null).toLowerCase()}
                  </span>
                </div>
              )}
              <div className="muted">{empresa ?? "—"} · researched {hace(d.created_at)}</div>
              {d.razon && <p className="why">{recortar(d.razon)}</p>}
              <form action={cambiarEstado} className="actions">
                <input type="hidden" name="id" value={p.id} />
                <button type="submit" name="estado" value="contactado" className="primary">
                  Contacted
                </button>
                <button type="submit" name="estado" value="descartado">Discard</button>
              </form>
            </div>
          );
        })}
      </div>
      <p className="more"><Link href="/personas">See all people →</Link></p>
    </>
  );
}
