// The journal: dreams and thoughts a reader sends the bot, kept for them and summed up on request.
//
// Each note is encrypted (AES-256-GCM with JOURNAL_KEY) before it leaves this function, so the
// database (Upstash Redis, connected through Vercel) only ever holds ciphertext. Note text never goes
// to logs, the repo or the website. The summary is plain counting, no AI.
// Files starting with "_" in api/ are not endpoints; telegram.js and journal.js use this one.
import { createCipheriv, createDecipheriv, createHmac, randomBytes, timingSafeEqual } from "node:crypto";

const DB_URL = process.env.KV_REST_API_URL ?? process.env.UPSTASH_REDIS_REST_URL;
const DB_TOKEN = process.env.KV_REST_API_TOKEN ?? process.env.UPSTASH_REDIS_REST_TOKEN;
const KEY = process.env.JOURNAL_KEY;
const WEBHOOK_SECRET = process.env.TELEGRAM_WEBHOOK_SECRET;
const TIMEZONE = process.env.JOURNAL_TZ ?? "Europe/London";   // the reader's clock, for dates and streaks
const MAX_NOTES = 10_000;
const LINK_MINUTES = 15;          // how long an /export link works
const FORGET_MINUTES = 10;        // how long the "Delete all" button works
const DAY = 86_400_000;

export const COMMANDS = new Set(["journal", "notes", "summary", "export", "forget"]);
const configured = Boolean(DB_URL && DB_TOKEN && KEY && WEBHOOK_SECRET);

// Only Telegram knows the webhook secret, so only its requests can read or change notes.
// (The bot's replies ride on the response, so an unchecked request could ask for someone's notes.)
export function trusted(request) {
  if (!configured) return false;
  const got = Buffer.from(request.headers.get("x-telegram-bot-api-secret-token") ?? "");
  const want = Buffer.from(WEBHOOK_SECRET);
  return got.length === want.length && timingSafeEqual(got, want);
}

// ---------------------------------------------------------------- storage

let keys;
const key = (purpose) => (keys ??= {
  notes: createHmac("sha256", KEY).update("journal notes").digest(),
  links: createHmac("sha256", KEY).update("journal links").digest(),
})[purpose];

function seal(chat, note) {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key("notes"), iv).setAAD(Buffer.from(String(chat)));
  const body = Buffer.concat([cipher.update(JSON.stringify(note), "utf8"), cipher.final()]);
  return Buffer.concat([iv, body, cipher.getAuthTag()]).toString("base64");
}

function unseal(chat, sealed) {
  const raw = Buffer.from(sealed, "base64");
  const decipher = createDecipheriv("aes-256-gcm", key("notes"), raw.subarray(0, 12))
    .setAAD(Buffer.from(String(chat)))
    .setAuthTag(raw.subarray(-16));
  return JSON.parse(Buffer.concat([decipher.update(raw.subarray(12, -16)), decipher.final()]).toString("utf8"));
}

// Upstash's REST API: several Redis commands in one request.
async function db(...commands) {
  const res = await fetch(`${DB_URL}/pipeline`, {
    method: "POST",
    headers: { Authorization: `Bearer ${DB_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(commands),
  });
  if (!res.ok) throw new Error(`database answered ${res.status}`);
  return (await res.json()).map((r) => {
    if (r.error) throw new Error(`database: ${r.error}`);
    return r.result;
  });
}

const ON = "journal:on";                       // the chats that keep a journal
const list = (chat) => `journal:${chat}`;      // one chat's notes, oldest first
// a note: { t: time in ms, k: "dream" | "thought" | "", s: text, m: Telegram message id }

export async function isOn(chat) {
  if (!configured) return false;
  const [on] = await db(["SISMEMBER", ON, String(chat)]);
  return on === 1;
}

// A plain message: a note if this chat keeps a journal, otherwise null.
export async function maybeNote(message) {
  try {
    if (!(await isOn(message.chat.id))) return null;
  } catch (err) {
    fail(err);
    return send(message.chat.id, S[langOf(message.from)].failed);
  }
  return onNote(message);
}

export async function allNotes(chat) {
  const [items] = await db(["LRANGE", list(chat), 0, -1]);
  return items.map((item) => unseal(chat, item));
}

// ---------------------------------------------------------------- words

const FORMS = {
  en: { note: ["note", "notes"], dream: ["dream", "dreams"], thought: ["thought", "thoughts"], day: ["day", "days"] },
  ru: {
    note: ["запись", "записи", "записей"], dream: ["сон", "сна", "снов"],
    thought: ["мысль", "мысли", "мыслей"], day: ["день", "дня", "дней"],
  },
};

function plural(lang, n, word) {
  const forms = FORMS[lang][word];
  if (lang === "en") return forms[n === 1 ? 0 : 1];
  const [d, h] = [n % 10, n % 100];
  return forms[d === 1 && h !== 11 ? 0 : d >= 2 && d <= 4 && (h < 10 || h >= 20) ? 1 : 2];
}
const many = (lang, n, word) => `${n} ${plural(lang, n, word)}`;

const S = {
  en: {
    intro: "🌙 <b>Your journal is on.</b>\n\n"
      + "Write here anything you’d like to keep: a dream, a thought, a line from the day. Every message becomes a note. "
      + "In the morning the paper will ask about your dreams; just reply to it.\n\n"
      + "🔒 <b>Who can read it.</b> Notes are encrypted before they are stored, and they never appear on the website "
      + "or in logs. The bot’s owner could technically get to them, and has promised not to read them.\n\n"
      + "/notes — latest notes\n/summary — your journal in numbers\n/export — everything, to read or save\n"
      + "/forget — delete everything\n/journal off — pause it (notes stay)",
    status: (n) => `🌙 <b>Your journal is on</b> · ${many("en", n, "note")}\n\n`
      + "/notes · /summary · /export · /forget\n/journal off — pause it\n\n"
      + "🔒 Notes are stored encrypted, never on the website or in logs.",
    paused: "Journal paused. Your notes are kept; /journal turns it back on.",
    notReady: "The journal isn’t switched on for this bot yet.",
    saved: "✍️ Saved. A dream or a thought?",
    savedDream: "🌙 Saved as a dream.",
    dream: "🌙 Dream",
    thought: "💭 Thought",
    marked: { dream: "🌙 Marked as a dream", thought: "💭 Marked as a thought" },
    gone: "That note isn’t there any more.",
    textOnly: "I can only keep text for now ✍️",
    full: "Your journal is full (10,000 notes). /export it, then /forget to start a fresh one.",
    failed: "⚠️ Couldn’t do that just now. Please try again in a minute.",
    empty: "No notes yet. Send /journal, then just write here.",
    hint: "Send /today for today’s paper 🗞\n/journal — keep your dreams and thoughts here 🌙",
    latest: "🌙 <b>Latest notes</b>",
    shown: (shown, total) => `${shown} of ${total} · everything: /export`,
    exported: (url) => `📖 <a href="${url}">Open your journal</a>\n`
      + `The link works for ${LINK_MINUTES} minutes. You can save it as a text file from there.`,
    forgetAsk: (n) => `This deletes all ${many("en", n, "note")} for good. There’s no undo.`,
    forgetButton: (n) => `Delete all ${n}`,
    forgotten: "Done. Your journal is empty now.",
    expired: "This button has expired. Send /forget again.",
    kinds: { dream: "Dream", thought: "Thought", "": "Note" },
    summary: "🌙 <b>Your journal</b>",
    total: (n, since) => `📝 ${many("en", n, "note")} since ${since}`,
    unmarked: (n) => `${n} unmarked`,
    month: (n, days) => `🗓 Last 30 days: ${many("en", n, "note")} · ${days} days of 30`,
    streak: (now, best) => now >= 2
      ? `🔥 ${now} days in a row${best > now ? ` (best: ${best})` : ""}`
      : `🔥 Best run: ${best} days in a row`,
    when: ["Mostly written at night", "Mostly written in the morning", "Mostly written in the afternoon",
      "Mostly written in the evening"],
    themes: { dream: "✨ Often in dreams", thought: "💭 Often in thoughts", all: "🔁 Often in notes" },
    ago: { 365: "A year ago", 30: "A month ago", 7: "A week ago" },
    early: "Patterns show up once there are a few more notes ✨",
    page: { title: "Journal", exported: "exported", download: "Download as text", expired:
      "This link has expired. Send /export to the bot for a new one." },
  },
  ru: {
    intro: "🌙 <b>Дневник включён.</b>\n\n"
      + "Пиши сюда всё, что хочется сохранить: сон, мысль, строчку из дня. Каждое сообщение станет записью. "
      + "По утрам газета будет спрашивать про сны — просто ответь на неё.\n\n"
      + "🔒 <b>Кто может прочитать.</b> Записи шифруются перед сохранением и не попадают ни на сайт, ни в логи. "
      + "Технически владелец бота может до них добраться, но обещает их не читать.\n\n"
      + "/notes — последние записи\n/summary — дневник в цифрах\n/export — всё целиком, прочитать или сохранить\n"
      + "/forget — удалить всё\n/journal off — поставить на паузу (записи останутся)",
    status: (n) => `🌙 <b>Дневник включён</b> · ${many("ru", n, "note")}\n\n`
      + "/notes · /summary · /export · /forget\n/journal off — поставить на паузу\n\n"
      + "🔒 Записи хранятся зашифрованными, их нет ни на сайте, ни в логах.",
    paused: "Дневник на паузе. Записи сохранены, /journal включит его снова.",
    notReady: "Дневник у этого бота ещё не включён.",
    saved: "✍️ Сохранено. Это сон или мысль?",
    savedDream: "🌙 Сохранено как сон.",
    dream: "🌙 Сон",
    thought: "💭 Мысль",
    marked: { dream: "🌙 Отмечено как сон", thought: "💭 Отмечено как мысль" },
    gone: "Этой записи уже нет.",
    textOnly: "Пока сохраняю только текст ✍️",
    full: "Дневник заполнен (10 000 записей). Сохрани его через /export, потом /forget — и можно начать новый.",
    failed: "⚠️ Сейчас не получилось. Попробуй ещё раз через минуту.",
    empty: "Записей пока нет. Отправь /journal и просто пиши сюда.",
    hint: "/today — сегодняшний выпуск 🗞\n/journal — дневник для снов и мыслей 🌙",
    latest: "🌙 <b>Последние записи</b>",
    shown: (shown, total) => `${shown} из ${total} · всё целиком: /export`,
    exported: (url) => `📖 <a href="${url}">Открыть дневник</a>\n`
      + `Ссылка работает ${LINK_MINUTES} минут. Там же можно скачать его текстовым файлом.`,
    forgetAsk: (n) => `Это навсегда удалит ${n === 1 ? "единственную запись" : `все ${many("ru", n, "note")}`}. Вернуть их будет нельзя.`,
    forgetButton: (n) => `Удалить ${n === 1 ? "запись" : `все ${n}`}`,
    forgotten: "Готово. Дневник пуст.",
    expired: "Эта кнопка устарела. Отправь /forget ещё раз.",
    kinds: { dream: "Сон", thought: "Мысль", "": "Запись" },
    summary: "🌙 <b>Твой дневник</b>",
    total: (n, since) => `📝 ${many("ru", n, "note")} с ${since}`,
    unmarked: (n) => `${n} без пометки`,
    month: (n, days) => `🗓 За 30 дней: ${many("ru", n, "note")} · ${days} ${plural("ru", days, "day")} из 30`,
    streak: (now, best) => now >= 2
      ? `🔥 ${many("ru", now, "day")} подряд${best > now ? ` (рекорд — ${best})` : ""}`
      : `🔥 Рекорд: ${many("ru", best, "day")} подряд`,
    when: ["Чаще всего пишешь ночью", "Чаще всего пишешь утром", "Чаще всего пишешь днём", "Чаще всего пишешь вечером"],
    themes: { dream: "✨ Часто в снах", thought: "💭 Часто в мыслях", all: "🔁 Часто в записях" },
    ago: { 365: "Год назад", 30: "Месяц назад", 7: "Неделю назад" },
    early: "Закономерности появятся, когда записей станет больше ✨",
    page: { title: "Дневник", exported: "выгружено", download: "Скачать текстом", expired:
      "Ссылка устарела. Отправь боту /export, чтобы получить новую." },
  },
};

export const langOf = (user) => (/^(ru|uk|be|kk)/.test(user?.language_code ?? "") ? "ru" : "en");
export const hint = (user) => S[langOf(user)].hint;
export const unavailable = (message) => send(message.chat.id, S[langOf(message.from)].notReady);
const ICON = { dream: "🌙", thought: "💭", "": "✍️" };

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const clip = (s, n) => (s.length > n ? `${s.slice(0, n).trimEnd()}…` : s);

// ---------------------------------------------------------------- dates (in the reader's time zone)

const locale = (lang) => (lang === "ru" ? "ru-RU" : "en-GB");
const format = (lang, t, options) => new Intl.DateTimeFormat(locale(lang), { timeZone: TIMEZONE, ...options }).format(t);
const dayOf = (t) => new Intl.DateTimeFormat("en-CA", { timeZone: TIMEZONE, dateStyle: "short" }).format(t);
const hourOf = (t) => Number(new Intl.DateTimeFormat("en-GB", { timeZone: TIMEZONE, hour: "2-digit", hourCycle: "h23" }).format(t));
const addDays = (day, n) => new Date(Date.parse(`${day}T00:00:00Z`) + n * DAY).toISOString().slice(0, 10);
const dateOf = (lang, t, now = Date.now()) =>
  format(lang, t, { day: "numeric", month: "long", ...(dayOf(t).slice(0, 4) !== dayOf(now).slice(0, 4) && { year: "numeric" }) });
const stamp = (lang, t) => format(lang, t, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// ---------------------------------------------------------------- telegram replies

const send = (chat_id, text, extra = {}) =>
  Response.json({ method: "sendMessage", chat_id, text, parse_mode: "HTML", link_preview_options: { is_disabled: true }, ...extra });
const toast = (callback_query_id, text, show_alert = false) =>
  Response.json({ method: "answerCallbackQuery", callback_query_id, text, show_alert });
const fail = (err) => console.error(`journal: ${err.message}`);    // never the note itself

// Any message that isn't a command, while the journal is on.
async function onNote(message) {
  const chat = message.chat.id;
  const s = S[langOf(message.from)];
  if (!message.text) return send(chat, s.textOnly);
  // A reply to the morning paper (which asks "Dreamt something? 🌙") is a dream.
  const reply = message.reply_to_message;
  const dream = Boolean(reply?.from?.is_bot && reply.text?.includes("🌙"));
  const note = { t: message.date * 1000, k: dream ? "dream" : "", s: message.text, m: message.message_id };
  try {
    const [count] = await db(["RPUSH", list(chat), seal(chat, note)]);
    if (count > MAX_NOTES) {
      await db(["RPOP", list(chat)]);
      return send(chat, s.full);
    }
    const at = `${count - 1}:${note.m}`;
    return send(chat, dream ? s.savedDream : s.saved, {
      reply_parameters: { message_id: message.message_id, allow_sending_without_reply: true },
      reply_markup: { inline_keyboard: [[
        { text: s.dream, callback_data: `kind:dream:${at}` },
        { text: s.thought, callback_data: `kind:thought:${at}` },
      ]] },
    });
  } catch (err) {
    fail(err);
    return send(chat, s.failed);
  }
}

// An edited message: the note follows the edit.
export async function onEdit(message) {
  if (!message.text || message.text.startsWith("/")) return new Response("ok");
  const chat = message.chat.id;
  try {
    const [length, items] = await db(["LLEN", list(chat)], ["LRANGE", list(chat), -200, -1]);
    const offset = length - items.length;
    for (let i = items.length - 1; i >= 0; i--) {
      const note = unseal(chat, items[i]);
      if (note.m === message.message_id) {
        await db(["LSET", list(chat), offset + i, seal(chat, { ...note, s: message.text })]);
        break;
      }
    }
  } catch (err) {
    fail(err);
  }
  return new Response("ok");
}

// The buttons under "Saved" and under /forget.
export async function onButton(query) {
  const s = S[langOf(query.from)];
  const chat = query.message?.chat?.id ?? query.from.id;
  const [what, ...rest] = (query.data ?? "").split(":");
  try {
    if (what === "kind") {
      const [kind, i, m] = rest;
      const [item] = await db(["LINDEX", list(chat), Number(i)]);
      const note = item && unseal(chat, item);
      if (!note || String(note.m) !== m || !(kind in s.marked)) return toast(query.id, s.gone);
      await db(["LSET", list(chat), Number(i), seal(chat, { ...note, k: kind })]);
      return toast(query.id, s.marked[kind]);
    }
    if (what === "forget") {
      if (Date.now() / 1000 - Number(rest[0]) > FORGET_MINUTES * 60) return toast(query.id, s.expired, true);
      await db(["DEL", list(chat)]);
      return toast(query.id, s.forgotten, true);
    }
  } catch (err) {
    fail(err);
    return toast(query.id, s.failed, true);
  }
  return toast(query.id, "");
}

// /journal, /notes, /summary, /export, /forget
export async function onCommand(message, command, arg, origin) {
  const chat = message.chat.id;
  const lang = langOf(message.from);
  const s = S[lang];
  if (!configured) return send(chat, s.notReady);
  try {
    if (command === "journal") {
      if (/^(off|stop|pause|выкл|стоп|пауза)/i.test(arg)) {
        await db(["SREM", ON, String(chat)]);
        return send(chat, s.paused);
      }
      const [added, count] = await db(["SADD", ON, String(chat)], ["LLEN", list(chat)]);
      return send(chat, added ? s.intro : s.status(count));
    }
    if (command === "notes") {
      const [count, items] = await db(["LLEN", list(chat)], ["LRANGE", list(chat), -5, -1]);
      if (!count) return send(chat, s.empty);
      const notes = items.map((item) => unseal(chat, item)).reverse();
      const body = notes.map((n) => `${ICON[n.k] ?? "✍️"} <b>${stamp(lang, n.t)}</b>\n${esc(clip(n.s, 700))}`);
      return send(chat, [s.latest, ...body, `<i>${s.shown(notes.length, count)}</i>`].join("\n\n"));
    }
    if (command === "summary") {
      const notes = await allNotes(chat);
      return send(chat, notes.length ? summarize(notes, lang) : s.empty);
    }
    if (command === "export") {
      const [count] = await db(["LLEN", list(chat)]);
      return send(chat, count ? s.exported(link(origin, chat, lang)) : s.empty);
    }
    if (command === "forget") {
      const [count] = await db(["LLEN", list(chat)]);
      if (!count) return send(chat, s.empty);
      const button = { text: s.forgetButton(count), callback_data: `forget:${Math.floor(Date.now() / 1000)}` };
      return send(chat, s.forgetAsk(count), { reply_markup: { inline_keyboard: [[button]] } });
    }
  } catch (err) {
    fail(err);
    return send(chat, s.failed);
  }
  return send(chat, s.hint);
}

// ---------------------------------------------------------------- the summary

// Words that say nothing about a dream: pronouns, linking words, "dream" itself, "yesterday"…
const STOP = new Set(`
a about above after again against ago all almost also always am among an and another any anyone anything are around as
at away back be became because become been before being below between both but by came can can't come could couldn't
did didn't do does doesn't doing don't done down during each else even ever every everything few for from get gets
getting got go goes going gone had hadn't has have haven't having he he's her here hers herself him himself his how i
i'd i'll i'm i've if in into is isn't it it's its itself just kind know knew last let like little lot made make many
maybe me might more most much must my myself never next no nor not nothing now of off oh ok okay on once one only or
other our ours out over own put quite rather really said same saw say says see seem seemed seems she she's should so
some somehow someone something sometimes somewhere still such sure than that that's the their them themselves then
there there's these they they're thing things think this those though thought through till to today told tomorrow too
took two under until up upon us very was wasn't way we we're well went were weren't what what's when where which while
who whom whose why will with within without won't would wouldn't yeah yes yesterday yet you you're your yours yourself
dream dreams dreamt dreamed dreaming tonight last night
а без более больше будет будто бы был была были было быть в вам вас ведь весь во вот впрочем все всегда всего всех всю
вы где да даже два для до другой его ее ей ему если есть еще ж же за зачем здесь и из или им ими их к как какая какой
какие какую когда кто куда ли лучше между меня мне много может можно мой моя мое мои мы на над надо наконец нас не него
нее ней нельзя нет ни нибудь никогда ним них ничего но ну о об один он она они оно опять от перед по под после потом
потому почти при про раз разве с сам свою себе себя сейчас со совсем так такой там тебе тебя тем теперь то тогда того
тоже только том тот три тут ты у уж уже хоть чего чем через что чтоб чтобы чуть эти этого этой этом этот эту это я
очень просто типа короче вообще кажется помню словно вроде какой-то что-то кто-то где-то как-то который которая которое
которые которых которую котором которой мной нам нами наш наша наше наши твой твоя твое твои свой своя свое свои сегодня
вчера завтра снова потому сначала затем всё ещё её сон сны сна сне сном снов снах снилось снился снилась снились
приснилось приснился приснилась приснились снится снятся сниться
және мен бен пен сен ол біз сіз олар бұл сол осы мына ана не неге қалай қашан қайда кім ғой ғана бір барлық әр еш бар
жоқ еді екен емес болды болып болса бола деп деді дейді үшін туралы сияқты секілді өте тым енді әлі кейін соң дейін бірақ
немесе яғни сонда мұнда онда маған саған оған бізге сізге оларға мені сені оны менің сенің оның біздің сіздің олардың өз
өзім өзі түс түсім түсімде түсіме түсімнен түстер бүгін кеше ертең сосын тағы
`.trim().split(/\s+/));

const tokens = (text) =>
  (text.toLowerCase().replace(/ё/g, "е").replace(/’/g, "'").match(/\p{L}+(?:'\p{L}+)*/gu) ?? [])
    .filter((w) => w.length >= 3 && !STOP.has(w));

function stemEn(w) {
  w = w.replace(/'s$/, "");
  if (w.length > 4 && w.endsWith("ies")) return `${w.slice(0, -3)}y`;
  if (w.length > 5 && w.endsWith("ing")) return w.slice(0, -3);
  if (w.length > 4 && w.endsWith("ed")) return w.slice(0, -2);
  if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss")) return w.slice(0, -1);
  return w;
}

// Kazakh adds endings one after another (үй → үйлер → үйлерде); peel up to three of them.
const KK_ENDINGS = `лардың лердің дардың дердің тардың тердің лардан лерден дардан дерден тардан терден ларға лерге дарға
дерге тарға терге ларда лерде дарда дерде тарда терде ларды лерді дарды дерді тарды терді ымыз іміз лар лер дар дер тар
тер ның нің дың дің тың тің нан нен дан ден тан тен нда нде мыз міз ңыз ңіз мен бен пен ға ге қа ке на не да де та те
ны ні ды ді ты ті ым ім ың ің сы сі`.split(/\s+/).sort((a, b) => b.length - a.length);

function stemKk(w) {
  for (let pass = 0; pass < 3; pass++) {
    const end = KK_ENDINGS.find((e) => w.endsWith(e) && w.length - e.length >= 2);    // roots can be short: үй, су
    if (!end) break;
    w = w.slice(0, -end.length);
  }
  return w;
}

// The Snowball stemmer for Russian (snowballstem.org/algorithms/russian/stemmer.html):
// летала, летать, летал → лета; воде, водой, вода → вод.
const RU = {
  gerund1: ["в", "вши", "вшись"],
  gerund2: ["ив", "ивши", "ившись", "ыв", "ывши", "ывшись"],
  adjective: "ее ие ые ое ими ыми ей ий ый ой ем им ым ом его ого ему ому их ых ую юю ая яя ою ею".split(" "),
  participle1: ["ем", "нн", "вш", "ющ", "щ"],
  participle2: ["ивш", "ывш", "ующ"],
  reflexive: ["ся", "сь"],
  verb1: "ла на ете йте ли й л ем н ло но ет ют ны ть ешь нно".split(" "),
  verb2: "ила ыла ена ейте уйте ите или ыли ей уй ил ыл им ым ен ило ыло ено ят ует уют ит ыт ены ить ыть ишь ую ю".split(" "),
  noun: "а ев ов ие ье е иями ями ами еи ии и ией ей ой ий й иям ям ием ем ам ом о у ах иях ях ы ь ию ью ю ия ья я".split(" "),
};
const VOWELS = "аеиоуыэюя";

// The longest ending from the groups; endings of a group marked true must follow а or я.
function cut(s, ...groups) {
  let best;
  for (const [endings, afterA] of groups) {
    for (const e of endings) if (s.endsWith(e) && (!best || e.length > best.e.length)) best = { e, afterA };
  }
  if (!best) return null;
  if (best.afterA && !["а", "я"].includes(s[s.length - best.e.length - 1])) return null;
  return s.slice(0, -best.e.length);
}

function stemRu(word) {
  const first = [...word].findIndex((c) => VOWELS.includes(c));
  if (first < 0) return word;
  const head = word.slice(0, first + 1);
  let rv = word.slice(first + 1);
  // step 1
  let r = cut(rv, [RU.gerund1, true], [RU.gerund2, false]);
  if (r === null) {
    rv = cut(rv, [RU.reflexive, false]) ?? rv;
    r = cut(rv, [RU.adjective, false]);
    if (r !== null) r = cut(r, [RU.participle1, true], [RU.participle2, false]) ?? r;
    else r = cut(rv, [RU.verb1, true], [RU.verb2, false]) ?? cut(rv, [RU.noun, false]);
  }
  rv = r ?? rv;
  // step 2
  if (rv.endsWith("и")) rv = rv.slice(0, -1);
  // step 3: -ость in R2
  const whole = head + rv;
  const r1 = regionAfter(whole, 0);
  const r2 = regionAfter(whole, r1);
  for (const e of ["ость", "ост"]) {
    if (whole.endsWith(e) && whole.length - e.length >= r2) {
      rv = rv.slice(0, -e.length);
      break;
    }
  }
  // step 4
  if (rv.endsWith("нн")) rv = rv.slice(0, -1);
  else if (rv.endsWith("ейше") || rv.endsWith("ейш")) {
    rv = rv.replace(/ейше?$/, "");
    if (rv.endsWith("нн")) rv = rv.slice(0, -1);
  } else if (rv.endsWith("ь")) rv = rv.slice(0, -1);
  return head + rv;
}

// The start of the region after the first non-vowel that follows a vowel (R1, and R2 inside it).
function regionAfter(w, from) {
  for (let i = from + 1; i < w.length; i++) {
    if (!VOWELS.includes(w[i]) && VOWELS.includes(w[i - 1])) return i + 1;
  }
  return w.length;
}

const KAZAKH = /[әғқңөұүһі]/;
const stemOf = (w) => (/[a-z]/.test(w) ? stemEn(w) : KAZAKH.test(w) ? stemKk(w) : stemRu(w));

// The words that come back in several notes, each shown in its most common form.
function themes(notes) {
  const found = new Map();
  for (const note of notes) {
    const seen = new Set();
    for (const word of tokens(note.s)) {
      const stem = stemOf(word);
      if (stem.length < (KAZAKH.test(word) ? 2 : 3)) continue;
      const entry = found.get(stem) ?? { notes: 0, forms: new Map() };
      entry.forms.set(word, (entry.forms.get(word) ?? 0) + 1);
      if (!seen.has(stem)) entry.notes++;
      seen.add(stem);
      found.set(stem, entry);
    }
  }
  return [...found.values()]
    .filter((e) => e.notes >= 2)
    .sort((a, b) => b.notes - a.notes)
    .slice(0, 6)
    .map((e) => `${[...e.forms].sort((a, b) => b[1] - a[1])[0][0]} (${e.notes})`);
}

export function summarize(notes, lang, now = Date.now()) {
  const s = S[lang];
  const lines = [s.summary, ""];
  const by = { dream: 0, thought: 0, "": 0 };
  for (const n of notes) by[n.k in by ? n.k : ""]++;
  const parts = [
    by.dream && many(lang, by.dream, "dream"),
    by.thought && many(lang, by.thought, "thought"),
    by[""] && (by.dream || by.thought) && s.unmarked(by[""]),
  ].filter(Boolean);
  lines.push(s.total(notes.length, dateOf(lang, notes[0].t, now)) + (parts.length ? `: ${parts.join(", ")}` : ""));

  const recent = notes.filter((n) => n.t >= now - 30 * DAY);
  if (notes[0].t < now - 30 * DAY && recent.length) {
    lines.push(s.month(recent.length, new Set(recent.map((n) => dayOf(n.t))).size));
  }

  // days in a row: up to today (or yesterday, if today has no note yet), and the best run ever
  const days = new Set(notes.map((n) => dayOf(n.t)));
  const today = dayOf(now);
  let current = 0;
  for (let d = days.has(today) ? today : addDays(today, -1); days.has(d); d = addDays(d, -1)) current++;
  let best = 0;
  let run = 0;
  let prev;
  for (const d of [...days].sort()) {
    run = prev && addDays(prev, 1) === d ? run + 1 : 1;
    best = Math.max(best, run);
    prev = d;
  }
  if (best >= 2) lines.push(s.streak(current, best));

  if (notes.length < 3) return [...lines, "", s.early].join("\n");

  const hours = [0, 0, 0, 0];      // night, morning, afternoon, evening
  for (const n of notes) {
    const h = hourOf(n.t);
    hours[h < 5 ? 0 : h < 12 ? 1 : h < 17 ? 2 : 3]++;
  }
  const top = hours.indexOf(Math.max(...hours));
  if (notes.length >= 5 && hours[top] / notes.length >= 0.4) lines.push(`⏰ ${s.when[top]}`);

  const groups = [["dream", notes.filter((n) => n.k === "dream")], ["thought", notes.filter((n) => n.k === "thought")]]
    .filter(([, group]) => group.length >= 3);
  if (!groups.length) groups.push(["all", notes]);
  const found = groups.map(([kind, group]) => [kind, themes(group)]).filter(([, words]) => words.length);
  if (found.length) lines.push("");
  for (const [kind, words] of found) lines.push(`${s.themes[kind]}: ${words.join(" · ")}`);
  if (!found.length && notes.length < 8) lines.push("", s.early);

  // a note from a year, a month or a week ago (give or take a day)
  for (const ago of [365, 30, 7]) {
    const target = Date.parse(addDays(today, -ago));
    const old = notes.find((n) => Math.abs(Date.parse(dayOf(n.t)) - target) <= DAY);
    if (old) {
      lines.push("", `↩️ ${s.ago[ago]}, ${dateOf(lang, old.t, now)}:`, `<i>${esc(clip(old.s, 240))}</i>`);
      break;
    }
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------- /export

const sign = (text) => createHmac("sha256", key("links")).update(text).digest("base64url");

function link(origin, chat, lang) {
  const until = Math.floor(Date.now() / 1000) + LINK_MINUTES * 60;
  return `${origin}/api/journal?c=${chat}&e=${until}&l=${lang}&s=${sign(`${chat}.${until}`)}`;
}

// The chat an /export link was made for, or null if the link is forged or out of date.
export function linkChat(params) {
  if (!configured) return null;
  const [chat, until, sig] = [params.get("c"), params.get("e"), params.get("s") ?? ""];
  if (!/^-?\d+$/.test(chat ?? "") || !/^\d+$/.test(until ?? "")) return null;
  const want = Buffer.from(sign(`${chat}.${until}`));
  const got = Buffer.from(sig);
  if (got.length !== want.length || !timingSafeEqual(got, want)) return null;
  return Number(until) >= Date.now() / 1000 ? chat : null;
}

export const expiredText = (lang) => S[lang === "ru" ? "ru" : "en"].page.expired;

export function asText(notes, lang) {
  const s = S[lang];
  const now = Date.now();
  const head = `${s.page.title} · ${many(lang, notes.length, "note")} · ${s.page.exported} ${format(lang, now, { dateStyle: "long" })}`;
  const body = notes.map((n) => `${s.kinds[n.k] ?? s.kinds[""]} · ${format(lang, n.t, { dateStyle: "full", timeStyle: "short" })}\n\n${n.s}`);
  return [head, ...body].join("\n\n――――――――\n\n") + "\n";
}

export function asPage(notes, lang, textUrl) {
  const s = S[lang];
  const now = Date.now();
  const articles = notes.map((n) => `
    <article class="${n.k || "note"}">
      <div class="when"><span class="kind">${ICON[n.k] ?? "✍️"} ${s.kinds[n.k] ?? s.kinds[""]}</span>
        <time>${format(lang, n.t, { dateStyle: "full", timeStyle: "short" })}</time></div>
      <p>${esc(n.s)}</p>
    </article>`).join("");
  return `<!doctype html>
<html lang="${lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${s.page.title}</title>
<style>
  :root { --bg: #fbf8f3; --ink: #1f1b16; --muted: #786f64; --line: #e8e0d4; --dream: #5a55c8; --thought: #b4630c; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #15130f; --ink: #eee7dc; --muted: #a2988b; --line: #2e2a23; --dream: #aaa6ff; --thought: #f0b25e; }
  }
  body { margin: 0; background: var(--bg); color: var(--ink); font: 18px/1.65 Georgia, "Times New Roman", serif; }
  main { max-width: 640px; margin: 0 auto; padding: 40px 16px 72px; }
  h1 { font-size: 40px; line-height: 1.1; margin: 0 0 8px; }
  .meta, .when { font: 14px/1.4 system-ui, -apple-system, sans-serif; color: var(--muted); }
  .meta a { color: inherit; }
  article { border-top: 1px solid var(--line); padding: 24px 0; }
  .when { display: flex; flex-wrap: wrap; gap: 4px 12px; }
  .kind { font-weight: 600; }
  .dream .kind { color: var(--dream); }
  .thought .kind { color: var(--thought); }
  p { margin: 8px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
</head>
<body>
<main>
  <h1>🌙 ${s.page.title}</h1>
  <div class="meta">${many(lang, notes.length, "note")} · ${s.page.exported} ${format(lang, now, { dateStyle: "long" })}
    · <a href="${textUrl}">${s.page.download}</a></div>
  ${articles}
</main>
</body>
</html>`;
}
