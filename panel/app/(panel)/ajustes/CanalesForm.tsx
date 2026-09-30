"use client";

import { useActionState } from "react";
import { guardarCanales } from "../acciones";

// Formulario cliente (useActionState, como TokenSlackForm) para mostrar inline
// el error de panel_guardar_canales. Los IDs no son secretos: el campo parte
// de la lista guardada para poder editarla en sitio.
export function CanalesForm({ actuales }: { actuales: string[] }) {
  const [state, formAction, pending] = useActionState(guardarCanales, {});

  return (
    <>
      <form action={formAction} className="inline">
        <input
          name="canales"
          type="text"
          autoComplete="off"
          placeholder="C0ABC123, C0DEF456"
          aria-label="Watched channel IDs"
          defaultValue={actuales.join(", ")}
          style={{ width: 360 }}
        />
        <button type="submit" disabled={pending}>
          {pending ? "Saving…" : "Save channels"}
        </button>
      </form>
      {state.error && <p className="notice">{state.error}</p>}
      {state.ok && !state.error && (
        <p className="muted">
          Saved. The worker picks it up on its next poll (a couple of minutes).
        </p>
      )}
      <p className="muted" style={{ fontSize: 12 }}>
        Find a channel&apos;s ID at the bottom of its details in Slack. The account behind the
        token must be a member of each channel. Comma-separated; up to 20. Saving an empty list
        falls back to the SLACK_CHANNEL_IDS environment variable, if set.
      </p>
    </>
  );
}
