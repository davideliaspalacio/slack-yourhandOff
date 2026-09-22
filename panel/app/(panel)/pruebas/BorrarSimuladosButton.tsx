"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { borrarSimulados } from "../acciones";

// panel_borrar_simulados no vuelve atrás, así que hace falta una segunda
// confirmación -- un `confirm()` del navegador es suficiente para un botón
// que solo ven las pocas personas de panel_users. No es un <form action>
// como el resto del panel porque necesita mostrar cuántas personas borró.
export function BorrarSimuladosButton() {
  const router = useRouter();
  const [state, setState] = useState<"idle" | "working" | "done" | "error">("idle");
  const [mensaje, setMensaje] = useState<string | null>(null);

  async function onClick() {
    if (!confirm("Delete all test data? This removes every test person and their messages.")) {
      return;
    }
    setState("working");
    const result = await borrarSimulados();
    if ("error" in result) {
      setState("error");
      setMensaje(result.error);
      return;
    }
    setState("done");
    setMensaje(
      result.borrados === 1
        ? "Deleted 1 test person."
        : `Deleted ${result.borrados} test people.`,
    );
    router.refresh();
  }

  return (
    <div>
      <button type="button" onClick={onClick} disabled={state === "working"}>
        {state === "working" ? "Deleting…" : "Delete all test data"}
      </button>
      {mensaje && <p className={state === "error" ? "notice" : "muted"}>{mensaje}</p>}
    </div>
  );
}
