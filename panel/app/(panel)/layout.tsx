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
        <Link href="/personas">People</Link>
        <Link href="/costes">Costs</Link>
        <Link href="/ajustes">Settings</Link>
        <span className="muted">{user.email}</span>
        <form action={salir}><button type="submit">Sign out</button></form>
      </nav>
      <main>
        {allowed ? children : (
          <div className="card">
            <h1>No access</h1>
            <p>Your account ({user.email}) isn&apos;t authorized to view this panel.
              Ask David to add it.</p>
          </div>
        )}
      </main>
    </>
  );
}
