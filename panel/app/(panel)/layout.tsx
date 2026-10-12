import Link from "next/link";
import { redirect } from "next/navigation";
import { acceso } from "@/lib/acceso";
import { SinAcceso } from "./SinAcceso";
import { salir } from "./acciones";

export default async function PanelLayout({ children }: { children: React.ReactNode }) {
  const { user, allowed } = await acceso();
  if (!user) redirect("/login");
  // Iniciar sesión no basta: la base solo enseña datos a los correos autorizados
  // (`allowed`). Cada ficha que puede dar 404 lo comprueba también por su cuenta.

  return (
    <>
      <nav>
        <span className="brand">Founders Club</span>
        <div className="links">
          <Link href="/">Today</Link>
          <Link href="/personas">People</Link>
          <Link href="/senales">Signals</Link>
          <Link href="/cuentas">Accounts</Link>
          <Link href="/costes">Costs</Link>
          <Link href="/ajustes">Settings</Link>
          <Link href="/pruebas">Test mode</Link>
        </div>
        <span className="muted who">{user.email}</span>
        <form action={salir}><button type="submit">Sign out</button></form>
      </nav>
      <main>
        {allowed ? children : <SinAcceso email={user.email} />}
      </main>
    </>
  );
}
