"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

const MINIMO = 10;

// Poner una contraseña nueva tras pulsar el enlace del correo.
//
// El enlace trae la sesión en el fragmento de la URL. El cliente de Supabase la recoge
// solo al cargar, así que aquí basta con esperar a que exista antes de dejar escribir:
// sin sesión, el cambio lo rechazaría el servidor.
export function NuevaClaveForm() {
  const router = useRouter();
  const [lista, setLista] = useState(false);
  const [clave, setClave] = useState("");
  const [repetida, setRepetida] = useState("");
  const [estado, setEstado] = useState<"idle" | "guardando" | "error">("idle");
  const [error, setError] = useState("");

  useEffect(() => {
    const supabase = createClient();
    supabase.auth.getSession().then(({ data }) => setLista(Boolean(data.session)));
    const { data: sub } = supabase.auth.onAuthStateChange((_evento, sesion) =>
      setLista(Boolean(sesion)),
    );
    return () => sub.subscription.unsubscribe();
  }, []);

  async function guardar(event: React.FormEvent) {
    event.preventDefault();
    if (clave !== repetida) {
      setError("The two passwords are not the same.");
      setEstado("error");
      return;
    }
    if (clave.length < MINIMO) {
      setError(`Use at least ${MINIMO} characters.`);
      setEstado("error");
      return;
    }
    setEstado("guardando");
    const { error: fallo } = await createClient().auth.updateUser({ password: clave });
    if (fallo) {
      setError(fallo.message);
      setEstado("error");
      return;
    }
    router.replace("/");
    router.refresh();
  }

  return (
    <main style={{ maxWidth: 420, marginTop: "12vh" }}>
      <div className="card">
        <h1>Set a new password</h1>
        {!lista ? (
          <p className="muted">
            This page only works from the link in your email, and the link expires after
            an hour. Ask for a new one from the sign-in page.
          </p>
        ) : (
          <form onSubmit={guardar} style={{ display: "grid", gap: 12 }}>
            <label htmlFor="clave" className="muted">New password</label>
            <input id="clave" type="password" required autoComplete="new-password"
                   value={clave} onChange={(e) => setClave(e.target.value)} />
            <label htmlFor="repetida" className="muted">Repeat it</label>
            <input id="repetida" type="password" required autoComplete="new-password"
                   value={repetida} onChange={(e) => setRepetida(e.target.value)} />
            <button className="primary" type="submit" disabled={estado === "guardando"}>
              {estado === "guardando" ? "Saving…" : "Save and sign in"}
            </button>
            {estado === "error" && <p className="muted">{error}</p>}
          </form>
        )}
      </div>
    </main>
  );
}
