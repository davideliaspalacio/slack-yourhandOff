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
