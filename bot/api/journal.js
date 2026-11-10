// The journal's two web addresses (the notes themselves live in _journal.js):
//   ?on=<chat>             does this chat keep a journal? notify.py asks, to add the morning dream question
//   ?c=…&e=…&l=…&s=…       a signed /export link: the whole journal as a page, or with &as=txt as a text file
import * as journal from "./_journal.js";

const PRIVATE = { "Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer" };

export async function GET(request) {
  const url = new URL(request.url);
  const params = url.searchParams;

  if (params.has("on")) {
    const on = /^-?\d+$/.test(params.get("on")) && await journal.isOn(params.get("on")).catch(() => false);
    return Response.json({ on }, { headers: PRIVATE });
  }

  const lang = params.get("l") === "ru" ? "ru" : "en";
  const chat = journal.linkChat(params);
  if (!chat) {
    return new Response(journal.expiredText(lang), { status: 410, headers: { ...PRIVATE, "Content-Type": "text/plain; charset=utf-8" } });
  }
  const notes = await journal.allNotes(chat);
  if (params.get("as") === "txt") {
    const day = new Date().toISOString().slice(0, 10);
    return new Response(journal.asText(notes, lang), {
      headers: { ...PRIVATE, "Content-Type": "text/plain; charset=utf-8", "Content-Disposition": `attachment; filename="journal-${day}.txt"` },
    });
  }
  params.set("as", "txt");
  return new Response(journal.asPage(notes, lang, `?${params}`), { headers: { ...PRIVATE, "Content-Type": "text/html; charset=utf-8" } });
}
