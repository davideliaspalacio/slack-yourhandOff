"use client";

import { useActionState } from "react";
import { ayudarResearch } from "../../acciones";

type Props = {
  id: string;
  discarded: boolean;
  contacted: boolean;
  empresa: string;
  dominio: string;
  links: string[];
  notas: string;
};

// Formulario cliente (necesita useActionState para mostrar el error de
// panel_ayudar_research -- un enlace inválido, notas demasiado largas -- sin
// recargar la página), igual que el antiguo CorregirWebForm que reemplaza.
// panel_ayudar_research (migración 0008) valida y guarda las cuatro columnas
// a la vez; este formulario solo pasa el estado actual y el error de vuelta.
export function AyudarResearchForm({
  id,
  discarded,
  contacted,
  empresa,
  dominio,
  links,
  notas,
}: Props) {
  const [state, formAction, pending] = useActionState(ayudarResearch, {});
  const disabled = discarded || pending;

  return (
    <form action={formAction} style={{ display: "grid", gap: 10 }}>
      <input type="hidden" name="id" value={id} />
      <label style={{ display: "grid", gap: 4 }}>
        Company name
        <input
          name="empresa"
          defaultValue={empresa}
          disabled={disabled}
          style={{ width: "100%" }}
        />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        Company website
        <input
          name="dominio"
          defaultValue={dominio}
          placeholder="company.com"
          disabled={disabled}
          style={{ width: "100%" }}
        />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        Extra links
        <textarea
          name="links"
          defaultValue={links.join("\n")}
          rows={3}
          disabled={disabled}
          placeholder="Up to 10: news, blog, team page, job posts, LinkedIn…"
          style={{ width: "100%" }}
        />
      </label>
      <label style={{ display: "grid", gap: 4 }}>
        Notes for the agent
        <textarea
          name="notas"
          defaultValue={notas}
          rows={3}
          maxLength={2000}
          disabled={disabled}
          placeholder="What the team knows about this person or company"
          style={{ width: "100%" }}
        />
      </label>
      <div>
        <button type="submit" disabled={disabled}>
          {pending ? "Saving…" : "Save and re-research"}
        </button>
      </div>
      {discarded && (
        <p className="muted">This person is discarded, so re-researching is disabled.</p>
      )}
      {!discarded && contacted && (
        <p className="muted">Contacted: research refreshes here, no new Slack card.</p>
      )}
      {state.error && <p className="notice">{state.error}</p>}
    </form>
  );
}
