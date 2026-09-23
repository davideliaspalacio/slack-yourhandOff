export const usd = (n: number | string | null | undefined, digits = 4) =>
  `$${Number(n ?? 0).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export const fecha = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" }) : "—";

// Valores tal cual se guardan en la base (RLS en supabase/migrations/0006_panel.sql).
export const ESTADOS = ["nuevo", "investigado", "incompleto", "contactado", "cliente", "descartado"];
// El panel puede fijar todos menos 'incompleto', que lo decide el research.
export const ESTADOS_EDITABLES = ["nuevo", "investigado", "contactado", "cliente", "descartado"];

// Etiquetas en inglés para lo que se guarda en español. Solo para mostrar:
// nunca se comparan ni se envían a la base, que sigue viendo los valores de arriba.
const ESTADO_LABELS: Record<string, string> = {
  nuevo: "New",
  investigado: "Researched",
  incompleto: "Incomplete",
  contactado: "Contacted",
  cliente: "Client",
  descartado: "Discarded",
};
export const estadoLabel = (estado: string) => ESTADO_LABELS[estado] ?? estado;

// research_jobs.reason (check en supabase/migrations/0004_slack_ingest.sql).
const REASON_LABELS: Record<string, string> = {
  mensaje: "Message",
  miembro_nuevo: "New member",
  manual: "Manual",
};
export const reasonLabel = (reason: string) => REASON_LABELS[reason] ?? reason;

// research_jobs.status.
const JOB_STATUS_LABELS: Record<string, string> = {
  pendiente: "Pending",
  en_curso: "In progress",
  hecho: "Done",
  fallido: "Failed",
};
export const jobStatusLabel = (status: string) => JOB_STATUS_LABELS[status] ?? status;

// deliveries.band.
const BAND_LABELS: Record<string, string> = {
  alta: "High",
  media: "Medium",
  baja: "Low",
};
export const bandLabel = (band: string) => BAND_LABELS[band] ?? band;

// agent_actions.action: solo se mapean los nombres conocidos (ver
// ledger.record_action en el resto del repo); lo demás se muestra tal cual.
const ACTION_LABELS: Record<string, string> = {
  run_budget: "Budget check",
  research_errores: "Research errors",
  seguimiento_cortado: "Follow-up cut short",
  seguimiento_descartado: "Follow-up discarded",
  research_incompleto: "Incomplete research",
  leer_sitio: "Site read",
  alerta_operativa: "Operational alert",
  buscar_ofertas: "Job search",
  enriquecimiento_fallido: "Enrichment failed",
  buscar_web: "Web search",
  boton_pulsado: "Button pressed",
  digest_detenido: "Digest stopped",
  digest_fallido: "Digest failed",
  digest_enviado: "Digest sent",
  entrega_detenida: "Delivery stopped",
  entrega_reserva_atascada: "Delivery reservation stuck",
  entrega_fallida: "Delivery failed",
  sms_detenido: "SMS stopped",
  sms_fallido: "SMS failed",
};
export const accionLabel = (action: string) => ACTION_LABELS[action] ?? action;

// Radar de cuentas objetivo (supabase/migrations/0012_radar_cuentas.sql).

// Hace cuánto, en inglés y en palabras ("3 hours ago", "yesterday").
const RELATIVO = new Intl.RelativeTimeFormat("en-US", { numeric: "auto" });
export const hace = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const segundos = (new Date(iso).getTime() - Date.now()) / 1000;
  const pasos: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 31536000], ["month", 2592000], ["week", 604800],
    ["day", 86400], ["hour", 3600], ["minute", 60],
  ];
  for (const [unidad, s] of pasos) {
    if (Math.abs(segundos) >= s) return RELATIVO.format(Math.round(segundos / s), unidad);
  }
  return "just now";
};

// Días enteros desde una fecha: cuánto lleva abierta una vacante.
export const diasDesde = (iso: string) =>
  Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 86400000));

// Mismo umbral que el score del radar ("abierta ≥ 30 días: +2", spec §5):
// a partir de ahí la antigüedad se enseña como señal.
export const DIAS_VACANTE_ANTIGUA = 30;

// target_accounts.source.
const FUENTE_CUENTA_LABELS: Record<string, string> = {
  manual: "Manual",
  csv: "CSV",
  hubspot: "HubSpot",
};
export const fuenteCuentaLabel = (source: string) => FUENTE_CUENTA_LABELS[source] ?? source;

// target_accounts.status.
const ESTADO_CUENTA_LABELS: Record<string, string> = {
  watching: "Watching",
  paused: "Paused",
};
export const estadoCuentaLabel = (status: string) => ESTADO_CUENTA_LABELS[status] ?? status;

// hiring_signals.sources.
const FUENTE_VACANTE_LABELS: Record<string, string> = {
  linkedin: "LinkedIn",
  indeed: "Indeed",
  careers: "Careers page",
};
export const fuenteVacanteLabel = (source: string) => FUENTE_VACANTE_LABELS[source] ?? source;

// hiring_signals.status.
const ESTADO_SENAL_LABELS: Record<string, string> = {
  new: "New",
  pursued: "Pursued",
  dismissed: "Dismissed",
  snoozed: "Snoozed",
  researching: "Researching",
  ready: "Ready",
  task_created: "Task created",
};
export const estadoSenalLabel = (status: string) => ESTADO_SENAL_LABELS[status] ?? status;

// Un snooze vencido vuelve a ser una señal nueva a ojos del panel; la base la
// sigue guardando como 'snoozed' hasta que alguien actúe sobre ella.
export const estadoSenalVisible = (s: { status: string; snoozed_until: string | null }) =>
  s.status === "snoozed" && (!s.snoozed_until || new Date(s.snoozed_until).getTime() <= Date.now())
    ? "new" : s.status;

// Qué botones admite cada estado guardado: lo mismo que comprueba
// panel_accion_senal, para no enseñar botones que la base rechazaría.
export const ACCIONES_SENAL: Record<string, readonly ("pursue" | "dismiss" | "snooze")[]> = {
  new: ["pursue", "dismiss", "snooze"],
  snoozed: ["pursue", "dismiss", "snooze"],
  pursued: ["dismiss", "snooze"],
  dismissed: ["pursue", "snooze"],
};
