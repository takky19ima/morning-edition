// Telegram webhook for The Morning Edition bot.
// /start or /today → KK's paper, /my → «Утро». The reply rides on the webhook response itself
// (Telegram runs it as a Bot API call), so this function never needs the bot token.
const SITE = process.env.PAPER_URL ?? "https://takky19ima.github.io/morning-edition/";
const PAPERS = { start: "", today: "", my: "my/" };     // command → the paper's folder on the site

const reply = (chat_id, fields) => Response.json({ method: "sendMessage", chat_id, ...fields });

export async function POST(request) {
  const update = await request.json().catch(() => ({}));
  const message = update.message;
  if (!message?.text) return new Response("ok");

  const chat = message.chat.id;
  // Shows up in Vercel's logs — that's how a new reader's chat id is found for the daily list.
  console.log(`chat ${chat} (${message.chat.first_name ?? message.chat.title ?? "?"}) sent ${message.text.slice(0, 24)}`);

  const command = message.text.match(/^\/(\w+)/)?.[1];
  if (!(command in PAPERS)) {
    return reply(chat, { text: "Send /today for today’s paper 🗞" });
  }
  const paper = SITE + PAPERS[command];
  try {
    const res = await fetch(`${paper}telegram.json?${Date.now()}`);
    const { text, pdf, caption } = await res.json();
    if (pdf && caption) {
      // Telegram fetches the PDF from the site itself and posts it with the short summary under it.
      return Response.json({ method: "sendDocument", chat_id: chat, document: paper + pdf, caption, parse_mode: "HTML" });
    }
    return reply(chat, { text, parse_mode: "HTML", link_preview_options: { url: paper, prefer_large_media: true } });
  } catch {
    return reply(chat, { text: paper });
  }
}
