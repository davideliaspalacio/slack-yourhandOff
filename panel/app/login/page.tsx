import { rutaSegura } from "@/lib/ruta";
import { LoginForm } from "./LoginForm";

// `?next=` lo pone el proxy cuando alguien abre un enlace del panel (por ejemplo
// el botón "Open in panel" de Slack) sin sesión: tras entrar se vuelve ahí.
export default async function Login(props: PageProps<"/login">) {
  const { next } = (await props.searchParams) as Record<string, string | undefined>;
  return <LoginForm destino={rutaSegura(next)} />;
}
