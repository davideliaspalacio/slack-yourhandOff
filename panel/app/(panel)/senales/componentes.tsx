import { ACCIONES_SENAL, DIAS_VACANTE_ANTIGUA, diasDesde, estadoSenalVisible, fecha } from "@/lib/format";
import { accionSenal } from "../acciones";

// Piezas que comparten Signals y la ficha de una cuenta.

type SenalBadges = { reposted: boolean; first_seen_at: string; sources?: string[] };

// Lo que hace que una vacante puntúe (spec §5), a la vista: re-publicada,
// abierta hace mucho y, si se pide, vista en varias fuentes.
export function Badges({ s, fuentes = false }: { s: SenalBadges; fuentes?: boolean }) {
  const dias = diasDesde(s.first_seen_at);
  const n = s.sources?.length ?? 0;
  return (
    <>
      {s.reposted && <> <span className="pill signal">Reposted</span></>}
      {dias >= DIAS_VACANTE_ANTIGUA && <> <span className="pill signal">Open {dias}d</span></>}
      {fuentes && n >= 2 && <> <span className="pill">{n} sources</span></>}
    </>
  );
}

const ETIQUETA_ACCION = { pursue: "Pursue", dismiss: "Dismiss", snooze: "Snooze" } as const;

// Los botones que admite el estado guardado de la vacante. `volver` es la
// página (con sus filtros) a la que se regresa después del clic.
export function AccionesSenal({ s, volver }: {
  s: { id: string; status: string; snoozed_until: string | null };
  volver: string;
}) {
  const acciones = ACCIONES_SENAL[s.status] ?? [];
  const dormida = estadoSenalVisible(s) === "snoozed";
  return (
    <>
      {dormida && <div className="muted" style={{ fontSize: 12 }}>Snoozed until {fecha(s.snoozed_until)}</div>}
      {acciones.length ? (
        <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
          {acciones.map((accion) => (
            <form key={accion} action={accionSenal} className="inline">
              <input type="hidden" name="id" value={s.id} />
              <input type="hidden" name="accion" value={accion} />
              <input type="hidden" name="volver" value={volver} />
              <button type="submit" className={accion === "pursue" ? "primary" : ""}>
                {ETIQUETA_ACCION[accion]}
              </button>
            </form>
          ))}
        </span>
      ) : <span className="muted">—</span>}
    </>
  );
}

// El aviso o el error que deja una acción en la query (ver volverA en acciones.ts).
export function Avisos({ aviso, error }: { aviso?: string; error?: string }) {
  return (
    <>
      {aviso && <p className="notice">{aviso}</p>}
      {error && <p className="notice error">{error}</p>}
    </>
  );
}
