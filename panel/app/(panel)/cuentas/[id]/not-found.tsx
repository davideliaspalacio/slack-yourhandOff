import Link from "next/link";

// Misma idea que el not-found del grupo, pero para una cuenta del radar.
export default function NotFound() {
  return (
    <div className="card">
      <h1>Not found</h1>
      <p>We couldn&apos;t find that account. It may have been removed.</p>
      <p><Link href="/cuentas">← Accounts</Link></p>
    </div>
  );
}
