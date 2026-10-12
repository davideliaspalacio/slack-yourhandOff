import Link from "next/link";

// Lo que se ve cuando una ficha llama a notFound(): la persona está autorizada
// pero la fila no existe (p. ej. se borró con "Delete simulated").
export default function NotFound() {
  return (
    <div className="card">
      <h1>Not found</h1>
      <p>We couldn&apos;t find that person. They may have been deleted.</p>
      <p><Link href="/personas">← People</Link></p>
    </div>
  );
}
