"use client";

import { useActionState, useState, useTransition } from "react";
import { fecha } from "@/lib/format";
import { crearTokenMcp, revocarTokenMcp } from "../acciones";

export type TokenMcp = {
  id: string; nombre: string; sufijo: string;
  creado_at: string; ultimo_uso_at: string | null; revocado_at: string | null;
};

// "Connect your Claude": tokens personales del MCP remoto (solo lectura). El token
// en claro solo existe en el estado de este componente, justo después de crearlo:
// no se guarda en el navegador ni se puede pedir de nuevo a la base.
export function ConectarClaude({ tokens, url }: { tokens: TokenMcp[]; url: string }) {
  const [state, formAction, pending] = useActionState(crearTokenMcp, {});
  const [visto, setVisto] = useState<string | null>(null);
  const [revocando, startRevocar] = useTransition();
  const [errorRevocar, setErrorRevocar] = useState<string | null>(null);
  const [copiado, setCopiado] = useState(false);

  const token = state.token && state.token !== visto ? state.token : null;
  const activos = tokens.filter((t) => !t.revocado_at).length;

  function revocar(t: TokenMcp) {
    if (!confirm(`Revoke "${t.nombre}"? Any Claude using it stops working immediately.`)) return;
    startRevocar(async () => {
      const res = await revocarTokenMcp(t.id);
      setErrorRevocar(res.error ?? null);
    });
  }

  async function copiar(texto: string) {
    try {
      await navigator.clipboard.writeText(texto);
      setCopiado(true);
    } catch {
      setCopiado(false); // sin portapapeles: se puede seleccionar a mano
    }
  }

  const comando = token
    ? `claude mcp add --transport http handoff ${url} --header "Authorization: Bearer ${token}"`
    : "";

  return (
    <>
      {token && (
        <div className="notice">
          <p style={{ marginTop: 0 }}>
            <b>Copy your token now.</b> This is the only time it will be shown; it can&apos;t be
            retrieved later. If you lose it, revoke it and create a new one.
          </p>
          <p>
            <code style={{ wordBreak: "break-all" }}>{token}</code>{" "}
            <button type="button" onClick={() => copiar(token)}>
              {copiado ? "Copied" : "Copy token"}
            </button>
          </p>
          <p>
            MCP URL: <code style={{ wordBreak: "break-all" }}>{url}</code>
          </p>
          <p style={{ marginBottom: 0 }}>
            In Claude Code, run this once (it already includes your token):
            <br />
            <code style={{ wordBreak: "break-all" }}>{comando}</code>
          </p>
          <p style={{ marginBottom: 0 }}>
            <button type="button" onClick={() => { setVisto(token); setCopiado(false); }}>
              I&apos;ve saved it
            </button>
          </p>
        </div>
      )}

      {tokens.length > 0 ? (
        <table>
          <thead>
            <tr><th>Name</th><th>Ends in</th><th>Created</th><th>Last used</th><th /></tr>
          </thead>
          <tbody>
            {tokens.map((t) => (
              <tr key={t.id} className={t.revocado_at ? "muted" : undefined}>
                <td>{t.nombre}</td>
                <td><code>{t.sufijo}</code></td>
                <td>{fecha(t.creado_at)}</td>
                <td>{t.ultimo_uso_at ? fecha(t.ultimo_uso_at) : "Never"}</td>
                <td>
                  {t.revocado_at ? (
                    <span>Revoked {fecha(t.revocado_at)}</span>
                  ) : (
                    <button type="button" onClick={() => revocar(t)} disabled={revocando}>
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">You have no tokens yet.</p>
      )}
      {errorRevocar && <p className="notice">{errorRevocar}</p>}

      <form action={formAction} className="inline">
        <input
          name="nombre"
          type="text"
          autoComplete="off"
          placeholder="e.g. Anthony's Claude"
          aria-label="Token name"
          maxLength={60}
          required
          style={{ width: 260 }}
        />
        <button type="submit" disabled={pending || activos >= 5}>
          {pending ? "Creating…" : "Create token"}
        </button>
      </form>
      {state.error && <p className="notice">{state.error}</p>}
      <p className="muted" style={{ fontSize: 12 }}>
        MCP URL: <code>{url}</code> · Read-only: Claude can look things up (people, dossiers,
        signals, costs, system status) but can&apos;t change anything. Up to 5 active tokens per
        person; the database keeps only a fingerprint of each one.
      </p>
    </>
  );
}
