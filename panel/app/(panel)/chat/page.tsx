import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { canalLabel, empresaVisible, enlaceDm } from "@/lib/format";
import {
  AlcanceCanal, AlcancePrivado, Barra, Mensajes, ResumenDm,
} from "./componentes";
import {
  LIMITE_MAXIMO, LIMITE_POR_OMISION, autoresDe, esCanalId, esDmId, leerAutores,
  leerCanal, leerInterruptores, leerMensajesDm, listarCanales, listarDms,
} from "./datos";
import type { Autor, Canal, Dm, Interruptores, Supabase } from "./datos";
import css from "./chat.module.css";

// Las conversaciones del Founders Club, leídas desde el panel.
//
// SOLO LECTURA. El Founders Club es el Slack de otra gente y el agente entra con la
// sesión de AG: aquí no hay forma de escribir, reaccionar ni borrar, y no la va a
// haber en este fichero. El envío (en el Slack de Handoff) lo construye otra tanda.
//
// La conversación elegida va en la URL (`/chat?c=C06KEQR7DJQ`) para que se pueda
// enlazar y pegar en un mensaje, y para que la página se sirva ya renderizada.

export default async function Chat(props: PageProps<"/chat">) {
  const { c, n } = (await props.searchParams) as Record<string, string | undefined>;
  const supabase = await createClient();

  const interruptores = await leerInterruptores(supabase);
  const [canales, dms] = await Promise.all([
    listarCanales(supabase, interruptores),
    listarDms(supabase),
  ]);

  // Solo se abre lo que está en la barra. Un `?c=` con cualquier otro código no se
  // consulta: la pantalla enseña las conversaciones que el agente vigila, no un canal
  // cualquiera escrito a mano en la URL.
  const pedida = (c ?? "").trim().toUpperCase();
  const canal = esCanalId(pedida) ? canales.find((x) => x.id === pedida) : undefined;
  const dm = esDmId(pedida) ? dms.find((x) => x.id === pedida) : undefined;
  const elegida = canal ?? dm;
  const desconocida = pedida !== "" && !elegida;

  // La ventana crece de 100 en 100 con "Load earlier messages", con tope.
  const pedido = Number(n);
  const limite = Number.isFinite(pedido) && pedido > 0
    ? Math.min(Math.ceil(pedido / LIMITE_POR_OMISION) * LIMITE_POR_OMISION, LIMITE_MAXIMO)
    : LIMITE_POR_OMISION;

  return (
    <>
      <h1>Chat <span className="muted">— Founders Club, read-only</span></h1>
      <div className={`${css.layout} ${elegida ? css.elegida : ""}`}>
        <Barra canales={canales} dms={dms} seleccion={elegida?.id ?? null} />

        <div className={css.panel}>
          <Link href="/chat" className={css.volver}>← All conversations</Link>

          {desconocida && (
            <p className="notice">
              That conversation is not one the agent watches, so there is nothing to
              show. Pick one from the list.
            </p>
          )}

          {!elegida && !desconocida && (
            <p className="muted">
              Pick a conversation on the left. Public channels are read by everyone in
              the Founders Club; direct messages are one-to-one with AG.
            </p>
          )}

          {canal && (
            <PanelCanal
              supabase={supabase} canal={canal} limite={limite} interruptores={interruptores}
            />
          )}
          {dm && <PanelDm supabase={supabase} dm={dm} interruptores={interruptores} />}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------------
// Un canal público
// ---------------------------------------------------------------------------------

async function PanelCanal(
  { supabase, canal, limite, interruptores }:
  { supabase: Supabase; canal: Canal; limite: number; interruptores: Interruptores },
) {
  const { pagina, error } = await leerCanal(supabase, canal.id, limite);
  const autores = await leerAutores(supabase, autoresDe(pagina.mensajes));

  return (
    <>
      <header className={css.cabecera}>
        <h2 className={css.titulo}>
          {canalLabel(canal.id, canal.nombre)}{" "}
          <span className={`${css.etiqueta} ${css.publico}`}>Public</span>
        </h2>
        <p className={css.subtitulo}>{canal.id}</p>
      </header>
      <AlcanceCanal />

      {error && <p className="notice error">Couldn&apos;t load messages: {error}</p>}

      {!interruptores.leerHilos && (
        <p className="muted">
          Thread replies are not being read right now, so a post may have replies in
          Slack and none here. Turn on &quot;Read thread replies&quot; in{" "}
          <Link href="/ajustes">Settings</Link>.
        </p>
      )}

      {pagina.mensajes.length === 0 ? (
        <p className="muted">
          {error
            ? "No messages could be read."
            : "No messages read in this channel yet. The agent only keeps what it read "
              + "after it started watching — there is no backfill."}
        </p>
      ) : (
        <>
          <Mensajes mensajes={pagina.mensajes} autores={autores} />
          <p className="more">
            {pagina.hayMas ? (
              limite >= LIMITE_MAXIMO ? (
                <span className="muted">
                  Showing the most recent {LIMITE_MAXIMO} messages. Older ones are in
                  Slack.
                </span>
              ) : (
                <Link href={`/chat?c=${canal.id}&n=${limite + LIMITE_POR_OMISION}`}>
                  Load earlier messages
                </Link>
              )
            ) : (
              <span className="muted">That is everything read in this channel.</span>
            )}
          </p>
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------------
// Una conversación directa
// ---------------------------------------------------------------------------------

async function PanelDm(
  { supabase, dm, interruptores }:
  { supabase: Supabase; dm: Dm; interruptores: Interruptores },
) {
  const texto = await leerMensajesDm(supabase, dm.id, interruptores);
  // En un DM `dm_mensajes.de` no es un id de Slack, es 'dueno' u 'otro' (0031): los
  // dos autores posibles se conocen de antemano, así que no hay que preguntar nada.
  const autores = new Map<string, Autor>([
    ["dueno", { slackUserId: "dueno", nombre: "AG", empresa: null, prospectId: null }],
    ["otro", {
      slackUserId: "otro",
      nombre: dm.nombre ?? dm.slackUserId,
      empresa: dm.empresa,
      prospectId: dm.prospectId,
    }],
  ]);
  const empresa = empresaVisible(dm.empresa);
  const enlace = enlaceDm(dm.teamId, dm.id);

  return (
    <>
      <header className={css.cabecera}>
        <h2 className={css.titulo}>
          {dm.nombre ?? dm.slackUserId}{" "}
          <span className={`${css.etiqueta} ${css.privado}`}>Private · 1:1</span>
        </h2>
        <p className={css.subtitulo}>
          {empresa ? `${empresa} · ` : ""}{dm.slackUserId}
          {dm.prospectId && <> · <Link href={`/personas/${dm.prospectId}`}>Open profile</Link></>}
        </p>
        <ResumenDm dm={dm} />
      </header>
      <AlcancePrivado dm={dm} />

      {texto.estado === "apagado" && (
        <p className="notice">
          <b>Message text is off.</b> The agent tracks only who wrote last and when in
          direct messages — nothing that was said is stored. To store and show the text
          of AG&apos;s conversations with leads, turn on &quot;Reply suggestions&quot;
          under Direct message follow-ups in <Link href="/ajustes">Settings</Link>.
        </p>
      )}

      {texto.estado === "sin_permiso" && (
        <p className="notice error">
          <b>Message text is on, but this page cannot read it yet.</b> The text is
          stored for the agent only; the panel has not been granted access to it. That
          needs a database change — ask for it before relying on this view.
        </p>
      )}

      {texto.estado === "ok" && (
        texto.mensajes.length === 0
          ? <p className="muted">No messages stored for this conversation.</p>
          : <Mensajes mensajes={texto.mensajes} autores={autores} />
      )}

      {enlace && (
        <p><a href={enlace} target="_blank" rel="noreferrer">Open the conversation in Slack ↗</a></p>
      )}
    </>
  );
}
