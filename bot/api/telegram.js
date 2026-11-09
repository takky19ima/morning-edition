// Telegram webhook for The Morning Edition bot.
// /start or /today → today's paper. The reply rides on the webhook response itself
// (Telegram runs it as a Bot API call), so this function never needs the bot token.
const PAPER = process.env.PAPER_URL ?? "https://takky19ima.github.io/morning-edition/";

const reply = (chat_id, fields) => Response.json({ method: "sendMessage", chat_id, ...fields });

export async function POST(request) {
  const update = await request.json().catch(() => ({}));
  const message = update.message;
  if (!message?.text) return new Response("ok");

  const chat = message.chat.id;
  // Shows up in Vercel's logs — that's how a new reader's chat id is found for the daily list.
  console.log(`chat ${chat} (${message.chat.first_name ?? message.chat.title ?? "?"}) sent ${message.text.slice(0, 24)}`);

  if (!/^\/(start|today)\b/.test(message.text)) {
    return reply(chat, { text: "Send /today for today’s paper 🗞" });
  }
  try {
    const res = await fetch(`${PAPER}telegram.json?${Date.now()}`);
    const { text } = await res.json();
    return reply(chat, { text, parse_mode: "HTML", link_preview_options: { url: PAPER, prefer_large_media: true } });
  } catch {
    return reply(chat, { text: `Today’s paper: ${PAPER}` });
  }
}
