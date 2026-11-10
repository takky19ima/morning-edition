// Telegram webhook for The Morning Edition bot.
// /start or /today → KK's paper, /my → «Утро»; /journal and friends → the reader's journal (_journal.js).
// The reply rides on the webhook response itself (Telegram runs it as a Bot API call), so this
// function never needs the bot token.
import * as journal from "./_journal.js";

const SITE = process.env.PAPER_URL ?? "https://takky19ima.github.io/morning-edition/";
const PAPERS = { start: "", today: "", my: "my/" };     // command → the paper's folder on the site

const reply = (chat_id, fields) => Response.json({ method: "sendMessage", chat_id, ...fields });

export async function POST(request) {
  const update = await request.json().catch(() => ({}));
  // The journal only listens to requests that really come from Telegram.
  const trusted = journal.trusted(request);
  if (update.callback_query) return trusted ? journal.onButton(update.callback_query) : new Response("ok");
  if (update.edited_message) return trusted ? journal.onEdit(update.edited_message) : new Response("ok");
  const message = update.message;
  if (!message?.chat) return new Response("ok");

  const chat = message.chat.id;
  const [, command, arg = ""] = message.text?.match(/^\/(\w+)(?:@\w+)?\s*([\s\S]*)/) ?? [];
  if (command) {
    // Shows up in Vercel's logs — that's how a new reader's chat id is found for the daily list.
    // Only commands are logged: what people write to their journal never is.
    console.log(`chat ${chat} (${message.chat.first_name ?? message.chat.title ?? "?"}) sent /${command}`);
  }

  if (command in PAPERS) return paper(chat, PAPERS[command]);
  if (journal.COMMANDS.has(command)) {
    return trusted ? journal.onCommand(message, command, arg.trim(), new URL(request.url).origin) : journal.unavailable(message);
  }
  const note = !command && trusted && await journal.maybeNote(message);
  return note || reply(chat, { text: journal.hint(message.from) });
}

async function paper(chat, folder) {
  const url = SITE + folder;
  try {
    const res = await fetch(`${url}telegram.json?${Date.now()}`);
    const { text, pdf, caption } = await res.json();
    if (pdf && caption) {
      // Telegram fetches the PDF from the site itself and posts it with the short summary under it.
      return Response.json({ method: "sendDocument", chat_id: chat, document: url + pdf, caption, parse_mode: "HTML" });
    }
    return reply(chat, { text, parse_mode: "HTML", link_preview_options: { url, prefer_large_media: true } });
  } catch {
    return reply(chat, { text: url });
  }
}
