"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { createClient } from "@/lib/supabase/client";

// Correo y contraseña. No hay registro abierto: las cuentas se crean a mano en
// Supabase (Authentication → Users → Add user), y aun así solo entra quien
// esté en panel_users. Así nadie puede darse de alta con el correo de otro.
//
// Sí hay recuperación, porque sin ella cada contraseña olvidada obligaba a entrar al
// dashboard de Supabase. Pedirla NO dice si el correo existe: la respuesta es la misma
// en los dos casos, para que esta pantalla no sirva para averiguar quién tiene cuenta.
export function LoginForm({ destino }: { destino: string }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "error">("idle");
  const [recuperando, setRecuperando] = useState(false);
  const [recuperado, setRecuperado] = useState(false);

  async function signIn(event: React.FormEvent) {
    event.preventDefault();
    setState("sending");
    const { error } = await createClient().auth.signInWithPassword({
      email: email.trim().toLowerCase(),
      password,
    });
    if (error) {
      setState("error");
      return;
    }
    // La sesión ya está en las cookies: el proxy la lee en la siguiente petición.
    router.replace(destino);
    router.refresh();
  }

  async function recuperar() {
    const correo = email.trim().toLowerCase();
    if (!correo) return;
    setRecuperando(true);
    // El error se ignora a propósito: un fallo distinto según si el correo existe
    // convertiría esto en una forma de comprobar cuentas.
    await createClient().auth.resetPasswordForEmail(correo, {
      redirectTo: `${window.location.origin}/auth/nueva-clave`,
    });
    setRecuperando(false);
    setRecuperado(true);
  }

  return (
    <main style={{ maxWidth: 420, marginTop: "12vh" }}>
      <div className="card">
        <h1>Founders Club — Panel</h1>
        <form onSubmit={signIn} style={{ display: "grid", gap: 12 }}>
          <label htmlFor="email" className="muted">Email</label>
          <input id="email" type="email" required autoComplete="username" value={email}
                 onChange={(e) => setEmail(e.target.value)} placeholder="name@yourhandoff.com" />
          <label htmlFor="password" className="muted">Password</label>
          <input id="password" type="password" required autoComplete="current-password"
                 value={password} onChange={(e) => setPassword(e.target.value)} />
          <button className="primary" type="submit" disabled={state === "sending"}>
            {state === "sending" ? "Signing in…" : "Sign in"}
          </button>
          {state === "error" && <p className="muted">Incorrect email or password.</p>}
        </form>
        {recuperado ? (
          <p className="muted" style={{ marginTop: 12 }}>
            If that email has an account, a link to set a new password is on its way.
            Check your inbox.
          </p>
        ) : (
          <button type="button" onClick={recuperar} disabled={recuperando}
                  style={{ marginTop: 12, background: "none", border: "none", padding: 0,
                           textDecoration: "underline", cursor: "pointer" }}
                  className="muted">
            {recuperando ? "Sending…" : "Forgot your password?"}
          </button>
        )}
      </div>
    </main>
  );
}
