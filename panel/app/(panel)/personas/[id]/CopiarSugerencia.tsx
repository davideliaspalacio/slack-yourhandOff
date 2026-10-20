"use client";

import { useState } from "react";

// La respuesta sugerida para el DM (dm_sugerencias, 0031), lista para copiar y pegar en
// Slack. Componente cliente solo por el botón: el portapapeles vive en el navegador.
// El texto lo escribió un modelo leyendo una conversación con un tercero, así que se
// pinta siempre como texto (React lo escapa), nunca como HTML ni como enlace. El bloque
// también se puede seleccionar a mano si el navegador no deja usar el portapapeles.
export function CopiarSugerencia({ texto }: { texto: string }) {
  const [copiado, setCopiado] = useState<"si" | "error" | null>(null);

  async function copiar() {
    try {
      await navigator.clipboard.writeText(texto);
      setCopiado("si");
    } catch {
      setCopiado("error");
    }
    setTimeout(() => setCopiado(null), 2000);
  }

  return (
    <div>
      <pre
        style={{
          whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: "6px 0",
          padding: "10px 12px", border: "1px solid var(--line)", borderRadius: 8,
          userSelect: "all", fontFamily: "inherit",
        }}
      >
        {texto}
      </pre>
      <button type="button" onClick={copiar}>
        {copiado === "si" ? "Copied" : copiado === "error" ? "Select the text to copy" : "Copy reply"}
      </button>
    </div>
  );
}
