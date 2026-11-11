import { NuevaClaveForm } from "./NuevaClaveForm";

// Aquí aterriza el enlace de recuperación del correo. Supabase deja la sesión en el
// fragmento de la URL (`#access_token=…`), que el navegador NO manda al servidor: por eso
// esta pantalla es de cliente y no hay nada que leer aquí.
export default function NuevaClave() {
  return <NuevaClaveForm />;
}
