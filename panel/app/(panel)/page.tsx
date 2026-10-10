import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { empresaVisible, hace, haceDias, recortar } from "@/lib/format";
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
