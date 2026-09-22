"use client";

import Link from "next/link";
import { useActionState } from "react";
import { simularPersona } from "../acciones";

// Mismo patrón que AyudarResearchForm: useActionState para mostrar el error
// de panel_simular_persona (nombre/empresa vacíos, un enlace o dominio
// inválido, notas demasiado largas) sin recargar la página. La validación de
// verdad vive en la base (migración 0010_modo_pruebas.sql); este formulario
// solo junta los campos y pasa el error o el id de vuelta.
export function SimularForm() {
  const [state, formAction, pending] = useActionState(simularPersona, {});

  return (
    <form action={formAction} style={{ display: "grid", gap: 10 }} key={state.id ?? "form"}>
      <label style={{ display: "grid", gap: 4 }}>
        Full name
        <input name="nombre" disabled={pending} style={{ width: "100%" }} />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        Company
        <input name="empresa" disabled={pending} style={{ width: "100%" }} />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        Company website (optional)
        <input name="web" placeholder="company.com" disabled={pending} style={{ width: "100%" }} />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        Message they would have written in Slack (optional)
        <textarea
          name="mensaje"
          rows={3}
          disabled={pending}
          placeholder="What this person would have posted in the Founders Club"
          style={{ width: "100%" }}
        />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        Extra links (optional)
        <textarea
          name="links"
          rows={3}
          disabled={pending}
          placeholder="One per line, up to 10: news, blog, team page, job posts, LinkedIn…"
          style={{ width: "100%" }}
        />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        Notes for the agent (optional)
        <textarea
          name="notas"
          rows={3}
          maxLength={2000}
          disabled={pending}
          placeholder="Anything the team already knows about this person or company"
          style={{ width: "100%" }}
        />
      </label>
      <div>
        <button className="primary" type="submit" disabled={pending}>
          {pending ? "Creating…" : "Create and research"}
        </button>
      </div>
      {state.error && <p className="notice">{state.error}</p>}
      {state.id && !state.error && (
        <p className="notice">
          Created. The worker picks this up within ~2 minutes; refresh to follow it.{" "}
          <Link href={`/personas/${state.id}`}>View person</Link>
        </p>
      )}
    </form>
  );
}
