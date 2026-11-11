"use client";

import { useActionState, useEffect, useRef, useState, useTransition } from "react";
import { borrarTokenHubspot, guardarTokenHubspot } from "../acciones";

// Clonado de TokenSlackForm: mismo contrato (guardar, saber si hay una, borrar)
// y mismo formulario cliente (useActionState) para enseñar inline el error de
// panel_guardar_token_hubspot. El input se vacía al guardar: la llave no se
// queda en la pantalla.
export function TokenHubspotForm({ configurado }: { configurado: boolean }) {
  const [state, formAction, pending] = useActionState(guardarTokenHubspot, {});
  const [borrando, startBorrar] = useTransition();
  const [errorBorrar, setErrorBorrar] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    if (state.ok) formRef.current?.reset();
  }, [state]);

  function quitar() {
    if (!confirm("Remove the HubSpot key? Leads will stop reaching the CRM.")) return;
    startBorrar(async () => {
      const res = await borrarTokenHubspot();
      setErrorBorrar(res.error ?? null);
    });
  }

  return (
    <>
      <form ref={formRef} action={formAction} className="inline">
        <input
          name="token"
          type="password"
          autoComplete="off"
          placeholder="pat-na1-…"
          aria-label="HubSpot private app token"
          required
          style={{ width: 320 }}
        />
        <button type="submit" disabled={pending}>
          {pending ? "Saving…" : "Save key"}
        </button>
        {configurado && (
          <button type="button" onClick={quitar} disabled={borrando}>
            Remove key
          </button>
        )}
      </form>
      {state.error && <p className="notice">{state.error}</p>}
      {errorBorrar && <p className="notice">{errorBorrar}</p>}
      {state.ok && !state.error && (
        <p className="muted">
          Saved. The worker picks it up on its next pass (a few minutes).
        </p>
      )}
      <p className="muted" style={{ fontSize: 12 }}>
        The private app token from Handoff&apos;s own HubSpot portal, with access to
        companies, contacts, notes and properties. Stored write-only: the panel can
        update it but never read it back.
      </p>
    </>
  );
}
