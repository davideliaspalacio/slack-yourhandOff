export const usd = (n: number | string | null | undefined, digits = 4) =>
  `$${Number(n ?? 0).toFixed(digits)}`;

export const fecha = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("es-CO", { dateStyle: "medium", timeStyle: "short" }) : "—";

export const ESTADOS = ["nuevo", "investigado", "incompleto", "contactado", "cliente", "descartado"];
// El panel puede fijar todos menos 'incompleto', que lo decide el research.
export const ESTADOS_EDITABLES = ["nuevo", "investigado", "contactado", "cliente", "descartado"];
