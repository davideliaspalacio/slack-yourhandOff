import Link from "next/link";
import {
  canalLabel, diaDesdeTs, empresaVisible, fechaDesdeTs, hace, horaDesdeTs,
  respuestasLabel, ultimoDeLabel,
} from "@/lib/format";
import type { Autor, Canal, Dm, Mensaje } from "./datos";
import css from "./chat.module.css";

// Todo lo que se pinta en la vista de chat. Son componentes de servidor y no hay ni
// un formulario: esta pantalla solo lee (la fase siguiente añadirá el envío, en otros
// ficheros). Lo que ve el usuario va en inglés; los comentarios, en español.

// ---------------------------------------------------------------------------------
// Texto de terceros
// ---------------------------------------------------------------------------------

/** El texto de un mensaje, como texto y nada más.
 *
 * Lo escribió alguien del Founders Club, que no es nuestro workspace. Por eso:
 * nunca `dangerouslySetInnerHTML`, nunca Markdown y nunca se enlaza nada. React
 * escapa el contenido al pintarlo, así que `<script>` sale literalmente como
 * `<script>`; los saltos de línea los respeta el CSS con `white-space: pre-wrap`. */
export function Texto({ texto }: { texto: string | null }) {
  const limpio = (texto ?? "").trim();
  // Un mensaje puede no tener texto (un fichero, un adjunto): se dice, no se esconde.
  if (!limpio) return <p className={`${css.texto} ${css.vacio}`}>(no text)</p>;
  return <p className={css.texto}>{texto}</p>;
}

// ---------------------------------------------------------------------------------
// Quién escribió
// ---------------------------------------------------------------------------------

/** El autor de un mensaje: su nombre y empresa si los sabemos, con enlace a su ficha.
 *
 * Mucha gente del club no ha pasado por el research y no tiene fila en `prospects`:
 * entonces lo único que hay es su id de Slack, y es lo que se enseña. */
export function QuienEscribe(
  { userId, autores }: { userId: string | null; autores: Map<string, Autor> },
) {
  if (!userId) return <span className="muted">Unknown author</span>;
  const autor = autores.get(userId);
  const empresa = empresaVisible(autor?.empresa);

  return (
    <>
      <span className={css.quien}>
        {autor?.prospectId
          ? <Link href={`/personas/${autor.prospectId}`}>{autor.nombre ?? userId}</Link>
          : userId}
      </span>
      {empresa && <span className="muted">{empresa}</span>}
      {!autor && <span className="muted">no profile yet</span>}
    </>
  );
}

// ---------------------------------------------------------------------------------
// Mensajes
// ---------------------------------------------------------------------------------

function Fila(
  { m, autores }: { m: Mensaje; autores: Map<string, Autor> },
) {
  return (
    <div className={css.mensaje}>
      <div className={css.meta}>
        <QuienEscribe userId={m.userId} autores={autores} />
        <span className={css.hora}>{horaDesdeTs(m.ts)}</span>
      </div>
      <Texto texto={m.texto} />
      {m.respuestas.length > 0 && (
        // Un `details` basta: se abre y se cierra sin JavaScript y se renderiza en el
        // servidor, que es lo que esta pantalla necesita.
        <details className={css.hilo}>
          <summary>{respuestasLabel(m.respuestas.length)}</summary>
          <div className={css.respuestas}>
            {m.respuestas.map((r) => <Fila key={r.ts} m={r} autores={autores} />)}
          </div>
        </details>
      )}
    </div>
  );
}

/** Los mensajes de más antiguo a más nuevo, con un separador por jornada. */
export function Mensajes(
  { mensajes, autores }: { mensajes: Mensaje[]; autores: Map<string, Autor> },
) {
  // Se agrupa por jornada ANTES de pintar: el separador de día depende del mensaje
  // anterior, y resolverlo dentro del map obligaría a arrastrar una variable mutable
  // por el render.
  const jornadas: { dia: string; mensajes: Mensaje[] }[] = [];
  for (const m of mensajes) {
    const dia = diaDesdeTs(m.ts);
    const ultima = jornadas.at(-1);
    if (ultima?.dia === dia) ultima.mensajes.push(m);
    else jornadas.push({ dia, mensajes: [m] });
  }

  return (
    <div className={css.hilos}>
      {jornadas.map((j) => (
        <section key={j.dia}>
          <div className={css.dia}>{j.dia}</div>
          {j.mensajes.map((m) => <Fila key={m.ts} m={m} autores={autores} />)}
        </section>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------------
// Avisos de alcance: quién lee esto
// ---------------------------------------------------------------------------------

/** Lo que distingue un canal de un DM, dicho con palabras y no solo con un icono.
 *
 * Importa más de lo que parece: la fase siguiente añade el envío, y "esto lo lee todo
 * el club" frente a "esto lo lee una persona" no puede depender de fijarse en un #. */
export function AlcanceCanal() {
  return (
    <div className={`${css.alcance} ${css.publico}`}>
      <b>Public channel.</b>{" "}
      Everyone in the Founders Club can read this conversation — it is not ours, and
      the agent only reads it. Anything posted here is visible to the whole channel.
    </div>
  );
}

export function AlcancePrivado({ dm }: { dm: Dm }) {
  return (
    <div className={`${css.alcance} ${css.privado}`}>
      <b>Private direct message.</b>{" "}
      A one-to-one conversation between AG and {dm.nombre ?? dm.slackUserId}. Nobody
      else in the Founders Club can see it.
    </div>
  );
}

// ---------------------------------------------------------------------------------
// Barra lateral
// ---------------------------------------------------------------------------------

export function Barra(
  { canales, dms, seleccion }: { canales: Canal[]; dms: Dm[]; seleccion: string | null },
) {
  return (
    <aside className={css.barra}>
      <section className={`${css.grupo} ${css.publicos}`}>
        <h2 className={css.grupoTitulo}>Public channels</h2>
        <p className={css.grupoNota}>Read by everyone in the Founders Club.</p>
        {canales.length === 0 ? (
          <p className={css.grupoNota}>
            No channels are being watched. Pick them in{" "}
            <Link href="/ajustes">Settings</Link>.
          </p>
        ) : (
          <ul className={css.lista}>
            {canales.map((c) => (
              <li key={c.id}>
                <Link
                  href={`/chat?c=${c.id}`}
                  className={`${css.item} ${seleccion === c.id ? css.on : ""}`}
                >
                  <span className={css.nombre}>{canalLabel(c.id, c.nombre)}</span>
                  <span className={css.bajo}>
                    Public channel
                    {c.nombre ? "" : " — name not read yet"}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className={`${css.grupo} ${css.privados}`}>
        <h2 className={css.grupoTitulo}>Private direct messages</h2>
        <p className={css.grupoNota}>One-to-one with AG. Nobody else can see these.</p>
        {dms.length === 0 ? (
          <p className={css.grupoNota}>No direct conversations tracked yet.</p>
        ) : (
          <ul className={css.lista}>
            {dms.map((d) => (
              <li key={d.id}>
                <Link
                  href={`/chat?c=${d.id}`}
                  className={`${css.item} ${seleccion === d.id ? css.on : ""}`}
                >
                  <span className={css.nombre}>{d.nombre ?? d.slackUserId}</span>
                  <span className={css.bajo}>
                    Private · 1:1
                    {d.sinLeer ? ` · ${d.sinLeer} unread` : ""}
                    {" · "}{hace(fechaDesdeTs(d.ultimoTs))}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}

// ---------------------------------------------------------------------------------
// Cabecera de un DM y el aviso de que su texto no se enseña
// ---------------------------------------------------------------------------------

/** Lo que sí se sabe de un DM sin leer su texto: con quién y cuándo (dm_rastro, 0027). */
export function ResumenDm({ dm }: { dm: Dm }) {
  const cuando = fechaDesdeTs(dm.ultimoTs);
  return (
    <p className={css.subtitulo}>
      {ultimoDeLabel(dm.ultimoDe)}
      {cuando && <> · {hace(cuando)}</>}
      {dm.sinLeer ? <> · <span className={`${css.etiqueta} ${css.sinLeer}`}>
        {dm.sinLeer} unread
      </span></> : null}
    </p>
  );
}
