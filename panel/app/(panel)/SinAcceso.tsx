// Sesión válida pero correo fuera de panel_users. Lo usan el layout y las
// fichas, para decir lo mismo en los dos sitios.
export function SinAcceso({ email }: { email?: string | null }) {
  return (
    <div className="card">
      <h1>No access</h1>
      <p>Your account{email ? <> ({email})</> : null} isn&apos;t authorized to view this panel.
        Ask David to add this email. If you have another one on the list, sign out and sign
        in with that one.</p>
    </div>
  );
}
