import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import {
  DIAS_SENAL_RECIENTE, empresaVisible, hace, haceDias, recortar, tipoSenalLabel,
} from "@/lib/format";
import {
  EMOJI, ETIQUETAS, EXPLICACION, NIVELES, compararPrioridad, nivelPersona, type Nivel,
} from "@/lib/prioridad";
import { cambiarEstado } from "./acciones";

// La pantalla de entrada: qué mirar hoy. Las personas con encaje 2 o 3 que el
// agente investigó hace poco y nadie ha tocado todavía, lo más importante arriba
// (0039): el mismo orden que el resumen del día en Slack.
const DIAS = 14;
const MAXIMO = 25;
// Entre cuántas personas (las más recientes) se decide el orden. La prioridad pide tres
// consultas más por persona de la lista, y sus ids viajan en la URL: cien caben de sobra.
const PRESELECCION = 100;

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
  const recientes = ((data ?? []) as unknown as Fila[])
    .map((p) => ({ p, d: p.dossiers[0] }))
    .filter(({ d }) => d && Number(d.puntuacion ?? -1) >= 2
      && new Date(d.created_at).getTime() >= desde)
    .sort((a, b) => b.d.created_at.localeCompare(a.d.created_at))
    .slice(0, PRESELECCION);

  // Quién de la lista está esperando respuesta suya en un mensaje directo
  // (dm_rastro, 0027): el último mensaje de la conversación lo escribió la otra
  // persona. Del rastro solo sale el cuándo y el quién, nunca el texto.
  const esperando = new Set<string>();
  // Quién de la lista pidió ayuda en Slack esta semana: no es solo un research más.
  // La más reciente por persona (la consulta ya viene ordenada de más nueva a más vieja).
  const pidio = new Map<string, { tipo: string; confianza: string | null }>();
  // Con qué banda publicó el worker la tarjeta de cada dossier (deliveries, 0005). Es su
  // decisión tal cual -- ya tiene en cuenta la evidencia y la empresa sin confirmar --,
  // así que el panel no la vuelve a calcular a partir de la nota.
  const tarjetas = new Map<string, string>();
  if (recientes.length) {
    const ids = recientes.map(({ p }) => p.id);
    const [conversaciones, senales, entregas] = await Promise.all([
      supabase.from("dm_rastro")
        .select("slack_user_id, ultimo_de")
        .eq("ultimo_de", "otro")
        .in("slack_user_id", recientes.map(({ p }) => p.slack_user_id)),
      supabase.from("message_signals")
        .select("prospect_id, tipo, confianza")
        .in("prospect_id", ids)
        .eq("detectada", true)
        .gte("created_at", new Date(haceDias(DIAS_SENAL_RECIENTE)).toISOString())
        .order("created_at", { ascending: false }),
      supabase.from("deliveries")
        .select("prospect_id, band, dossier_version")
        .eq("kind", "slack")
        .in("prospect_id", ids),
    ]);
    for (const c of conversaciones.data ?? []) esperando.add(c.slack_user_id as string);
    for (const s of senales.data ?? []) {
      if (s.prospect_id && !pidio.has(s.prospect_id)) {
        pidio.set(s.prospect_id, { tipo: s.tipo ?? "", confianza: s.confianza ?? null });
      }
    }
    for (const e of entregas.data ?? []) {
      tarjetas.set(`${e.prospect_id}:${e.dossier_version}`, e.band as string);
    }
  }

  // El nivel de cada persona (lib/prioridad.ts, las mismas reglas que el worker) y el
  // orden: lo que más corre arriba. Se ordena antes de recortar, para que una persona
  // importante de hace diez días no se caiga de la lista por veinticinco de ayer.
  const candidatos = recientes
    .map(({ p, d }) => {
      const senal = pidio.get(p.id);
      const espera = esperando.has(p.slack_user_id);
      const nivel = nivelPersona({
        banda: tarjetas.get(`${p.id}:${d.version}`) ?? null,
        confianzaSenal: senal ? senal.confianza : null,
        esperando: espera,
      });
      return { p, d, senal, espera, nivel };
    })
    .sort((a, b) => compararPrioridad(
      { nivel: a.nivel, esperando: a.espera, pidio: Boolean(a.senal),
        nota: Number(a.d.puntuacion ?? 0), fecha: a.d.created_at },
      { nivel: b.nivel, esperando: b.espera, pidio: Boolean(b.senal),
        nota: Number(b.d.puntuacion ?? 0), fecha: b.d.created_at },
    ))
    .slice(0, MAXIMO);

  const porNivel = (nivel: Nivel) => candidatos.filter((c) => c.nivel === nivel);

  return (
    <>
      <h1>Today</h1>
      <div className="card intro">
        <p>
          Every time someone posts in the Founders Club Slack, the agent researches them and
          lists the ones worth a conversation here, most important first.
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
      {NIVELES.map((nivel) => {
        const grupo = porNivel(nivel);
        if (grupo.length === 0) return null;
        return (
          <section key={nivel}>
            <h3>
              {EMOJI[nivel]} {ETIQUETAS[nivel]}{" "}
              <span className="muted" style={{ fontWeight: 400 }}>
                — {EXPLICACION[nivel]} ({grupo.length})
              </span>
            </h3>
            <div className="cards">
              {grupo.map(({ p, d, senal, espera }) => {
                const empresa = empresaVisible(p.company_name);
                return (
                  <div key={p.id} className="card person">
                    <div className="head">
                      <Link href={`/personas/${p.id}`} className="name">
                        {p.full_name ?? p.slack_user_id}
                      </Link>
                      <span className={`pill fit-${d.puntuacion}`}>Fit {d.puntuacion}/3</span>
                    </div>
                    {(senal || espera) && (
                      <div>
                        {espera && <span className="pill awaiting">awaiting your reply</span>}
                        {espera && senal && " "}
                        {senal && (
                          <span className="pill asked">
                            asked: {tipoSenalLabel(senal.tipo || null).toLowerCase()}
                          </span>
                        )}
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
          </section>
        );
      })}
      <p className="more"><Link href="/personas">See all people →</Link></p>
    </>
  );
}
