import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Founders Club — Panel",
  description: "Personas investigadas, dossiers y costes del agente del Founders Club.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  );
}
