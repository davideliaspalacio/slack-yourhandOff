import Link from "next/link";
import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { salir } from "./acciones";

export default async function PanelLayout({ children }: { children: React.ReactNode }) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  // Iniciar sesión no basta: la base solo enseña datos a los correos autorizados.
  const { data: allowed } = await supabase.rpc("is_panel_user");

  return (
    <>
      <nav>
        <span className="brand">Founders Club</span>
        <Link href="/personas">Personas</Link>
        <Link href="/costes">Costes</Link>
        <Link href="/ajustes">Ajustes</Link>
        <span className="muted">{user.email}</span>
        <form action={salir}><button type="submit">Salir</button></form>
      </nav>
      <main>
        {allowed ? children : (
          <div className="card">
            <h1>Sin acceso</h1>
            <p>Tu cuenta ({user.email}) no está autorizada para ver este panel.
              Pídele a David que la añada.</p>
          </div>
        )}
      </main>
    </>
  );
}
