"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { createClient } from "@/lib/supabase/client";

// Correo y contraseña. No hay registro abierto: las cuentas se crean a mano en
// Supabase (Authentication → Users → Add user), y aun así solo entra quien
// esté en panel_users. Así nadie puede darse de alta con el correo de otro.
//
// Sí hay recuperación, porque sin ella cada contraseña olvidada obligaba a entrar al
// dashboard de Supabase. No hace falta esconder los errores para no filtrar qué correos
// existen: `resetPasswordForEmail` responde lo mismo exista o no la cuenta, eso ya lo
// garantiza Supabase. Tragárselos solo servía para que un límite de envío alcanzado se
// viera igual que un envío correcto, y el usuario esperara un correo que no iba a llegar.
export function LoginForm({ destino }: { destino: string }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "error">("idle");
  const [recuperando, setRecuperando] = useState(false);
  const [aviso, setAviso] = useState("");

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
    if (!correo) {
      setAviso("Type your email above first.");
      return;
    }
    setRecuperando(true);
    setAviso("");
    const { error: fallo } = await createClient().auth.resetPasswordForEmail(correo, {
      redirectTo: `${window.location.origin}/auth/nueva-clave`,
    });
    setRecuperando(false);
    if (fallo) {
      // El más común con diferencia: el envío de correos de Supabase va por horas.
      setAviso(
        /rate|limit|429/i.test(`${fallo.message} ${fallo.status ?? ""}`)
          ? "Too many emails requested. Wait an hour and try again."
          : `Could not send the email: ${fallo.message}`,
      );
      return;
    }
    setAviso("If that email has an account, a link to set a new password is on its way.");
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
        <button type="button" onClick={recuperar} disabled={recuperando}
                style={{ marginTop: 12, background: "none", border: "none", padding: 0,
                         textDecoration: "underline", cursor: "pointer" }}
                className="muted">
          {recuperando ? "Sending…" : "Forgot your password?"}
        </button>
        {aviso && <p className="muted" style={{ marginTop: 8 }}>{aviso}</p>}
      </div>
    </main>
  );
}
