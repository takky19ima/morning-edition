// The journal's door for the paper's web page (the notes themselves are handled in _journal.js).
// POST { action, ... } from the page, with "Authorization: Bearer <device key>" once it has one:
//   claim   { link }          → { device }   the one-time link from the morning Telegram message
//   add     { text, kind }    → { note, total }
//   recent  { all? }          → { notes, total }   the latest 10, or all of them
//   remove  { id }            → { removed, total }
//   forget                    → { removed, total }  deletes every note
//   summary                   → numbers for the "Look back" panel
import * as journal from "./_journal.js";

const SITE = new URL(process.env.PAPER_URL ?? "https://takky19ima.github.io/morning-edition/").origin;
const HEADERS = {
  "Access-Control-Allow-Origin": SITE,
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
  "Access-Control-Max-Age": "86400",
  "Cache-Control": "no-store",
  Vary: "Origin",
};
const answer = (body, status = 200) => Response.json(body, { status, headers: HEADERS });

export function OPTIONS() {
  return new Response(null, { status: 204, headers: HEADERS });
}

export async function POST(request) {
  if (!journal.configured) return answer({ error: "off" }, 503);
  const body = await request.json().catch(() => ({}));
  try {
    if (body.action === "claim") {
      const result = await journal.claim(body.link);
      return answer(result, result.error ? 403 : 200);
    }
    const chat = await journal.reader((request.headers.get("authorization") ?? "").replace(/^Bearer /, ""));
    if (!chat) return answer({ error: "unknown" }, 401);
    switch (body.action) {
      case "add": {
        const result = await journal.add(chat, body.text, body.kind);
        return answer(result, result.error ? 400 : 200);
      }
      case "recent": return answer(await journal.recent(chat, body.all ? 0 : 10));
      case "remove": return answer(await journal.remove(chat, String(body.id ?? "")));
      case "forget": return answer(await journal.forget(chat));
      case "summary": return answer(await journal.summary(chat));
    }
    return answer({ error: "action?" }, 400);
  } catch (err) {
    console.error(`journal: ${err.message}`);      // never the notes themselves
    return answer({ error: "failed" }, 500);
  }
}
