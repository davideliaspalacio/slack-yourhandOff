import { createClient } from "@/lib/supabase/server";
import { guardarUmbral } from "../acciones";

const EDITABLES: Record<string, string> = {
  banda_alta_min: "Encaje mínimo para banda alta (tarjeta + SMS)",
  banda_media_min: "Encaje mínimo para banda media (tarjeta)",
  banda_baja_min: "Encaje mínimo para banda baja (email diario). Súbelo para silenciar.",
  research_por_hora: "Investigaciones como máximo por hora",
  sms_por_dia: "SMS como máximo al día",
  sms_hora_inicio: "Hora desde la que se permiten SMS",
  sms_hora_fin: "Hora hasta la que se permiten SMS",
};

export default async function Ajustes() {
  const supabase = await createClient();
  const { data } = await supabase.from("config").select("key, value, updated_at")
    .in("key", Object.keys(EDITABLES));
  const valores = new Map((data ?? []).map((r) => [r.key, r.value]));

  return (
    <>
      <h1>Ajustes</h1>
      <p className="notice">El apagado de emergencia y los topes de dinero no se cambian desde aquí,
        a propósito: se tocan a mano en la base.</p>
      <table>
        <tbody>
          {Object.entries(EDITABLES).map(([key, label]) => (
            <tr key={key}>
              <td>{label}<div className="muted" style={{ fontSize: 12 }}>{key}</div></td>
              <td>
                <form action={guardarUmbral} className="inline">
                  <input type="hidden" name="key" value={key} />
                  <input name="value" type="number" min={0} step={1} style={{ width: 90 }}
                         defaultValue={String(valores.get(key) ?? "")} />
                  <button type="submit">Guardar</button>
                </form>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
