// Qué atender primero, en el panel (0039).
//
// Las reglas viven en el worker: `src/handoff_agent/delivery/prioridad.py`. El panel no
// puede importar Python, así que aquí está la parte que la pantalla Today necesita, con
// los mismos tres niveles y las mismas etiquetas (un test de Python,
// `tests/test_prioridad.py`, lee este archivo y falla si se separan). Si cambia una
// regla allí, se cambia aquí en el mismo commit.

export type Nivel = "ahora" | "hoy" | "fyi";

// De lo más urgente a lo menos: el orden de esta lista es el orden de la pantalla.
export const NIVELES: Nivel[] = ["ahora", "hoy", "fyi"];

export const ETIQUETAS: Record<Nivel, string> = {
  ahora: "ACT NOW",
  hoy: "TODAY",
  fyi: "FYI",
};

export const EMOJI: Record<Nivel, string> = { ahora: "🔴", hoy: "🟡", fyi: "⚪" };

export const EXPLICACION: Record<Nivel, string> = {
  ahora: "a likely customer is waiting",
  hoy: "worth a few minutes before the day ends",
  fyi: "good to know, nothing waiting on you",
};

export type DatosPersona = {
  // La banda con la que el worker publicó la tarjeta de su último dossier
  // (`deliveries.band`), o null si ese dossier no dio para tarjeta. Es la decisión del
  // worker tal cual: aquí no se vuelve a calcular con la nota.
  banda: string | null;
  // La confianza de lo último que pidió en Slack esta semana, o null si no pidió nada.
  confianzaSenal: string | null;
  // Si espera una respuesta suya en un mensaje directo.
  esperando: boolean;
};

// "Solo vacantes" (0040): la única prueba del encaje son vacantes publicadas, así que
// la ficha no tiene tarjeta ni SMS y sale en el reporte semanal de empresas contratando.
// Es la misma regla que `bands.va_al_reporte` en el worker, con sus dos excepciones: el
// interruptor `vacantes_solo_reporte` apagado y las personas del radar (`li:`). Today la
// necesita porque las tarjetas publicadas ANTES de la regla siguen en `deliveries`: sin
// esto seguirían contando como "act now".
export function soloVacantes(
  evidencia: string | null, slackUserId: string, reglaActiva: boolean,
): boolean {
  return reglaActiva && evidencia === "vacantes" && !slackUserId.startsWith("li:");
}

const orden = (nivel: Nivel) => NIVELES.indexOf(nivel);
const masUrgente = (niveles: Nivel[]): Nivel =>
  niveles.reduce((a, b) => (orden(b) < orden(a) ? b : a), "fyi" as Nivel);

// El nivel de una persona en Today: el de lo que más corre de todo lo que tiene
// abierto. Las mismas filas de la tabla de `prioridad.nivel`:
//   tarjeta de research     banda alta: ahora · media: hoy · sin tarjeta: fyi
//   "they asked"            confianza alta: ahora · resto: hoy
//   espera respuesta suya   lead caliente (banda alta o media): ahora · resto: hoy
export function nivelPersona({ banda, confianzaSenal, esperando }: DatosPersona): Nivel {
  const niveles: Nivel[] = [banda === "alta" ? "ahora" : banda === "media" ? "hoy" : "fyi"];
  if (confianzaSenal) niveles.push(confianzaSenal === "alta" ? "ahora" : "hoy");
  if (esperando) niveles.push(banda === "alta" || banda === "media" ? "ahora" : "hoy");
  return masUrgente(niveles);
}

// Para ordenar: primero el nivel; dentro del nivel, quien espera respuesta, luego quien
// pidió algo, luego la nota más alta y, a igualdad de todo, lo más reciente.
export function compararPrioridad(
  a: { nivel: Nivel; esperando: boolean; pidio: boolean; nota: number; fecha: string },
  b: { nivel: Nivel; esperando: boolean; pidio: boolean; nota: number; fecha: string },
): number {
  return orden(a.nivel) - orden(b.nivel)
    || Number(b.esperando) - Number(a.esperando)
    || Number(b.pidio) - Number(a.pidio)
    || b.nota - a.nota
    || b.fecha.localeCompare(a.fecha);
}
