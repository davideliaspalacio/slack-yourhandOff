"use client";

import { useState } from "react";
import { createClient } from "@/lib/supabase/client";

export default function Login() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");

  async function send(event: React.FormEvent) {
    event.preventDefault();
    setState("sending");
    const { error } = await createClient().auth.signInWithOtp({
      email: email.trim().toLowerCase(),
      options: { emailRedirectTo: `${window.location.origin}/auth/callback` },
    });
    setState(error ? "error" : "sent");
  }

  return (
    <main style={{ maxWidth: 420, marginTop: "12vh" }}>
      <div className="card">
        <h1>Founders Club — Panel</h1>
        {state === "sent" ? (
          <p>Te hemos enviado un enlace para entrar. Revisa tu correo.</p>
        ) : (
          <form onSubmit={send} style={{ display: "grid", gap: 12 }}>
            <label htmlFor="email" className="muted">Tu correo de trabajo</label>
            <input id="email" type="email" required value={email}
                   onChange={(e) => setEmail(e.target.value)} placeholder="nombre@yourhandoff.com" />
            <button className="primary" type="submit" disabled={state === "sending"}>
              {state === "sending" ? "Enviando…" : "Enviarme un enlace"}
            </button>
            {state === "error" && (
              <p className="muted">No se pudo enviar el enlace. Inténtalo de nuevo en un minuto.</p>
            )}
          </form>
        )}
      </div>
    </main>
  );
}
