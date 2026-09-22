import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Founders Club — Panel",
  description: "Researched people, dossiers, and costs for the Founders Club agent.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
