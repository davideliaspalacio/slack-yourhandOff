"use client";

import { useActionState } from "react";
import { corregirWeb } from "../../acciones";

// Formulario cliente aparte porque necesita useActionState para mostrar el
// error de panel_corregir_web (dominio inválido, por ejemplo) sin recargar la
// página: el resto del panel usa server actions "a ciegas" (sin leer su
// resultado), pero aquí sí hace falta.
export function CorregirWebForm({ id, discarded }: { id: string; discarded: boolean }) {
  const [state, formAction, pending] = useActionState(corregirWeb, {});

  return (
    <form action={formAction} className="inline">
      <input type="hidden" name="id" value={id} />
      <input name="dominio" placeholder="company.com" disabled={discarded || pending} />
      <button type="submit" disabled={discarded || pending}>
        {pending ? "Saving…" : "Save and re-research"}
      </button>
      {discarded && (
        <p className="muted">This person is discarded, so re-researching is disabled.</p>
      )}
      {state.error && <p className="notice">{state.error}</p>}
    </form>
  );
}
