"use client";

import { useActionState, useEffect, useRef, useState, useTransition } from "react";
import { borrarTokenSlack, guardarTokenSlack } from "../acciones";

// Formulario cliente (useActionState, como AyudarResearchForm) para mostrar
// inline el error de panel_guardar_token_slack. El input se vacía al guardar:
// el token no se queda en la pantalla.
export function TokenSlackForm({ configurado }: { configurado: boolean }) {
  const [state, formAction, pending] = useActionState(guardarTokenSlack, {});
  const [borrando, startBorrar] = useTransition();
  const [errorBorrar, setErrorBorrar] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    if (state.ok) formRef.current?.reset();
  }, [state]);

  function quitar() {
    if (!confirm("Remove the Founders Club token? The agent will stop reading Slack.")) return;
    startBorrar(async () => {
      const res = await borrarTokenSlack();
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
          placeholder="xoxp-…"
          required
          style={{ width: 320 }}
        />
        <button type="submit" disabled={pending}>
          {pending ? "Saving…" : "Save token"}
        </button>
        {configurado && (
          <button type="button" onClick={quitar} disabled={borrando}>
            Remove token
          </button>
        )}
      </form>
      {state.error && <p className="notice">{state.error}</p>}
      {errorBorrar && <p className="notice">{errorBorrar}</p>}
      {state.ok && !state.error && (
        <p className="muted">
          Saved. The worker picks it up on its next poll (a couple of minutes).
        </p>
      )}
      <p className="muted" style={{ fontSize: 12 }}>
        Paste the User OAuth Token (xoxp-…) from the Slack app installed in the Founders Club
        workspace. It is stored write-only: the panel can update it but never read it back.
      </p>
    </>
  );
}
