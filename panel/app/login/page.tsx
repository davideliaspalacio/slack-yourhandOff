"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { createClient } from "@/lib/supabase/client";

// Correo y contraseña. No hay registro abierto: las cuentas se crean a mano en
// Supabase (Authentication → Users → Add user), y aun así solo entra quien
// esté en panel_users. Así nadie puede darse de alta con el correo de otro.
export default function Login() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "error">("idle");

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
    router.replace("/personas");
    router.refresh();
  }

  return (
    <main style={{ maxWidth: 420, marginTop: "12vh" }}>
      <div className="card">
        <h1>Founders Club — Panel</h1>
        <form onSubmit={signIn} style={{ display: "grid", gap: 12 }}>
          <label htmlFor="email" className="muted">Correo</label>
          <input id="email" type="email" required autoComplete="username" value={email}
                 onChange={(e) => setEmail(e.target.value)} placeholder="nombre@yourhandoff.com" />
          <label htmlFor="password" className="muted">Contraseña</label>
          <input id="password" type="password" required autoComplete="current-password"
                 value={password} onChange={(e) => setPassword(e.target.value)} />
          <button className="primary" type="submit" disabled={state === "sending"}>
            {state === "sending" ? "Entrando…" : "Entrar"}
          </button>
          {state === "error" && <p className="muted">Correo o contraseña incorrectos.</p>}
        </form>
      </div>
    </main>
  );
}
