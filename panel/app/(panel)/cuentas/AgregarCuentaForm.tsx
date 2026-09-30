"use client";

import { useActionState } from "react";
import { agregarCuenta } from "../acciones";

// Mismo patrón que SimularForm: useActionState para enseñar el error de
// panel_agregar_cuenta (nombre vacío, dominio o id de LinkedIn inválidos) sin
// recargar. Si va bien, la acción redirige a la ficha de la cuenta.
export function AgregarCuentaForm() {
  const [state, formAction, pending] = useActionState(agregarCuenta, {});

  return (
    // La key cambia con cada respuesta: el formulario se vuelve a montar con
    // los valores que devolvió la acción en vez de quedarse vacío.
    <form action={formAction} style={{ display: "grid", gap: 10 }} key={JSON.stringify(state)}>
      <label style={{ display: "grid", gap: 4 }}>
        Company name
        <input name="nombre" required defaultValue={state.nombre} disabled={pending} style={{ width: "100%" }} />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        Domain (optional)
        <input name="dominio" defaultValue={state.dominio} placeholder="company.com" disabled={pending} style={{ width: "100%" }} />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        LinkedIn company ID (optional)
        <input name="linkedin" defaultValue={state.linkedin} inputMode="numeric" placeholder="16300" disabled={pending}
               style={{ width: "100%" }} />
      </label>
      <p className="muted" style={{ margin: 0, fontSize: 13 }}>
        Leave the LinkedIn ID empty and the radar looks it up on its next scan.
      </p>
      <div>
        <button className="primary" type="submit" disabled={pending}>
          {pending ? "Adding…" : "Add account"}
        </button>
      </div>
      {state.error && <p className="notice error">{state.error}</p>}
    </form>
  );
}
