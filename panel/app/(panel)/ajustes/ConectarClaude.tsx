"use client";

import { useActionState, useState, useTransition } from "react";
import { fecha } from "@/lib/format";
import { crearTokenMcp, revocarSesionOauth, revocarTokenMcp } from "../acciones";

export type TokenMcp = {
  id: string; nombre: string; sufijo: string;
  creado_at: string; ultimo_uso_at: string | null; revocado_at: string | null;
};

// Una sesión OAuth (panel_sesiones_oauth, 0026): alguien entró con su correo y su
// contraseña desde Claude, sin copiar ninguna llave. `cliente` es como se llama el
// programa que lo pidió -- texto de un tercero, así que se pinta como texto y nada más.
export type SesionOauth = {
  id: string; cliente: string; client_id: string;
  creado_at: string; ultimo_uso_at: string | null; revocado_at: string | null;
};

// Las dos formas de conectar un Claude, en una sola lista: la sesión que se abre desde
// "Add custom connector" (OAuth, migración 0026) y el token que se copia a mano (0023,
// el que sigue haciendo falta para `claude mcp add` en la terminal). Se muestran juntas
// porque para quien mira son lo mismo -- "qué tiene acceso a mis datos" -- y lo que
// cambia es de dónde viene cada una, que es la columna "How".
type Conexion = {
  clave: string; id: string; nombre: string; origen: "oauth" | "token"; detalle: string;
  creado_at: string; ultimo_uso_at: string | null; revocado_at: string | null;
};

// "Connect your Claude": leer, y cambiar estados, notas y umbrales (Fase 2, migración
// 0024). El token en claro solo existe en el estado de este componente, justo después de
// crearlo: no se guarda en el navegador ni se puede pedir de nuevo a la base.
export function ConectarClaude({
  tokens,
  sesiones,
  url,
}: {
  tokens: TokenMcp[];
  sesiones: SesionOauth[];
  url: string;
}) {
  const [state, formAction, pending] = useActionState(crearTokenMcp, {});
  const [visto, setVisto] = useState<string | null>(null);
  const [revocando, startRevocar] = useTransition();
  const [errorRevocar, setErrorRevocar] = useState<string | null>(null);
  const [copiado, setCopiado] = useState(false);
  const [copiadaUrl, setCopiadaUrl] = useState(false);
  const [manual, setManual] = useState(false);

  const token = state.token && state.token !== visto ? state.token : null;
  const activos = tokens.filter((t) => !t.revocado_at).length;

  const conexiones: Conexion[] = [
    ...sesiones.map((s) => ({
      clave: `oauth:${s.id}`, id: s.id, nombre: s.cliente, origen: "oauth" as const,
      detalle: "Signed in with your email",
      creado_at: s.creado_at, ultimo_uso_at: s.ultimo_uso_at, revocado_at: s.revocado_at,
    })),
    ...tokens.map((t) => ({
      clave: `token:${t.id}`, id: t.id, nombre: t.nombre, origen: "token" as const,
      detalle: `Pasted token ending in ${t.sufijo}`,
      creado_at: t.creado_at, ultimo_uso_at: t.ultimo_uso_at, revocado_at: t.revocado_at,
    })),
  ].sort((a, b) => b.creado_at.localeCompare(a.creado_at));

  function revocar(c: Conexion) {
    const aviso = c.origen === "oauth"
      ? `Disconnect "${c.nombre}"? It stops working immediately and whoever uses it will `
        + "have to sign in again."
      : `Revoke "${c.nombre}"? Any Claude using it stops working immediately.`;
    if (!confirm(aviso)) return;
    startRevocar(async () => {
      const res = c.origen === "oauth"
        ? await revocarSesionOauth(c.id)
        : await revocarTokenMcp(c.id);
      setErrorRevocar(res.error ?? null);
    });
  }

  async function copiar(texto: string, marcar: (ok: boolean) => void) {
    try {
      await navigator.clipboard.writeText(texto);
      marcar(true);
    } catch {
      marcar(false); // sin portapapeles: se puede seleccionar a mano
    }
  }

  const comando = token
    ? `claude mcp add --transport http handoff ${url} --header "Authorization: Bearer ${token}"`
    : "";

  return (
    <>
      <p>
        In Claude, open <b>Settings &rarr; Connectors &rarr; Add custom connector</b>, paste
        this URL and press <b>Connect</b>. Claude will ask you to sign in with the same email
        and password you use here. Nothing to copy, and it works in the browser and on your
        phone.
      </p>
      <p>
        <code style={{ wordBreak: "break-all" }}>{url}</code>{" "}
        <button type="button" onClick={() => copiar(url, setCopiadaUrl)}>
          {copiadaUrl ? "Copied" : "Copy URL"}
        </button>
      </p>

      {token && (
        <div className="notice">
          <p style={{ marginTop: 0 }}>
            <b>Copy your token now.</b> This is the only time it will be shown; it can&apos;t be
            retrieved later. If you lose it, revoke it and create a new one.
          </p>
          <p>
            <code style={{ wordBreak: "break-all" }}>{token}</code>{" "}
            <button type="button" onClick={() => copiar(token, setCopiado)}>
              {copiado ? "Copied" : "Copy token"}
            </button>
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

      {conexiones.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>Name</th><th>How</th><th>Created</th><th>Last used</th><th />
            </tr>
          </thead>
          <tbody>
            {conexiones.map((c) => (
              <tr key={c.clave} className={c.revocado_at ? "muted" : undefined}>
                <td>{c.nombre}</td>
                <td>{c.detalle}</td>
                <td>{fecha(c.creado_at)}</td>
                <td>{c.ultimo_uso_at ? fecha(c.ultimo_uso_at) : "Never"}</td>
                <td>
                  {c.revocado_at ? (
                    <span>
                      {c.origen === "oauth" ? "Disconnected" : "Revoked"} {fecha(c.revocado_at)}
                    </span>
                  ) : (
                    <button type="button" onClick={() => revocar(c)} disabled={revocando}>
                      {c.origen === "oauth" ? "Disconnect" : "Revoke"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">Nothing is connected yet.</p>
      )}
      {errorRevocar && <p className="notice">{errorRevocar}</p>}

      <p className="muted" style={{ fontSize: 12 }}>
        Any connection lets Claude read everything the panel shows and change statuses,
        research notes and thresholds, with the same access you have here. Every change is
        recorded under your name. It can&apos;t delete anything, touch the spending caps or
        shut the system down.
      </p>
      <p>
        <button type="button" onClick={() => setManual(!manual)}>
          {manual ? "Hide the manual way" : "Connect by hand instead"}
        </button>
      </p>

      {manual && (
        <>
          <p className="muted" style={{ fontSize: 12 }}>
            Only needed for Claude Code in a terminal, which asks for a token instead of
            signing in. Up to 5 active tokens per person; the database keeps only a
            fingerprint of each one.
          </p>
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
        </>
      )}
    </>
  );
}
