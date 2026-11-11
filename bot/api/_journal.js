// The journal: dreams and thoughts a reader writes at the end of her paper's web page.
//
// Each note is encrypted (AES-256-GCM with JOURNAL_KEY) before it leaves this function, so the
// database (Upstash Redis, connected through Vercel) only ever holds ciphertext. Note text never goes
// to logs, the repo or the website. The summary is plain counting, no AI.
//
// Who is writing: every morning notify.py puts a personal one-time link into each reader's Telegram
// message (#t=chat.until.signature, signed with JOURNAL_LINK_SECRET, which GitHub also has). The page
// trades it for a device key it keeps in the browser. Each link works once, so a forwarded message
// doesn't open someone else's journal.
// Files starting with "_" in api/ are not endpoints; journal.js uses this one.
import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";

const DB_URL = process.env.KV_REST_API_URL ?? process.env.UPSTASH_REDIS_REST_URL;
const DB_TOKEN = process.env.KV_REST_API_TOKEN ?? process.env.UPSTASH_REDIS_REST_TOKEN;
const KEY = process.env.JOURNAL_KEY;
const LINK_SECRET = process.env.JOURNAL_LINK_SECRET;
const TIMEZONE = process.env.JOURNAL_TZ ?? "Europe/London";   // the reader's clock, for streaks and hours
const MAX_NOTES = 10_000;
const MAX_LENGTH = 8_000;                                      // characters in one note
const DEVICE_DAYS = 180;                                       // a browser stays connected this long after its last visit
const DAY = 86_400_000;

export const configured = Boolean(DB_URL && DB_TOKEN && KEY && LINK_SECRET);

// ---------------------------------------------------------------- storage

let noteKey;
const notesKey = () => (noteKey ??= createHmac("sha256", KEY).update("journal notes").digest());

function seal(chat, note) {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", notesKey(), iv).setAAD(Buffer.from(String(chat)));
  const body = Buffer.concat([cipher.update(JSON.stringify(note), "utf8"), cipher.final()]);
  return Buffer.concat([iv, body, cipher.getAuthTag()]).toString("base64");
}

function unseal(chat, sealed) {
  const raw = Buffer.from(sealed, "base64");
  const decipher = createDecipheriv("aes-256-gcm", notesKey(), raw.subarray(0, 12))
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

const list = (chat) => `journal:${chat}`;                       // one reader's notes, oldest first
const device = (token) => `journal:device:${createHash("sha256").update(token).digest("hex")}`;
// a note: { id, t: time in ms, k: "dream" | "thought", s: text }

// ---------------------------------------------------------------- who is writing

const equal = (a, b) => a.length === b.length && timingSafeEqual(Buffer.from(a), Buffer.from(b));

// A morning link → a device key for this browser. Errors: "expired", "used", "invalid".
export async function claim(link) {
  const [chat, until, sig] = String(link ?? "").split(".");
  if (!/^-?\d+$/.test(chat ?? "") || !/^\d+$/.test(until ?? "") || !sig) return { error: "invalid" };
  const want = createHmac("sha256", LINK_SECRET).update(`claim.${chat}.${until}`).digest("base64url");
  if (!equal(sig, want)) return { error: "invalid" };
  if (Number(until) * 1000 < Date.now()) return { error: "expired" };
  const [fresh] = await db(["SET", `journal:claim:${sig}`, "1", "NX", "EXAT", Number(until)]);
  if (fresh !== "OK") return { error: "used" };
  const token = randomBytes(24).toString("base64url");
  await db(["SET", device(token), chat, "EX", DEVICE_DAYS * 86_400]);
  return { device: token };
}

// The reader a device key belongs to (and keep it alive), or null.
export async function reader(token) {
  if (!token || token.length > 100) return null;
  const [chat] = await db(["GET", device(token)], ["EXPIRE", device(token), DEVICE_DAYS * 86_400]);
  return chat ?? null;
}

// ---------------------------------------------------------------- notes

export async function add(chat, text, kind) {
  const s = String(text ?? "").trim().slice(0, MAX_LENGTH);
  if (!s) return { error: "empty" };
  const note = { id: randomBytes(6).toString("hex"), t: Date.now(), k: kind === "thought" ? "thought" : "dream", s };
  const [count] = await db(["RPUSH", list(chat), seal(chat, note)]);
  if (count > MAX_NOTES) {
    await db(["RPOP", list(chat)]);
    return { error: "full" };
  }
  return { note, total: count };
}

// The latest n notes (all of them without n), newest first.
export async function recent(chat, n) {
  const [total, items] = await db(["LLEN", list(chat)], ["LRANGE", list(chat), n ? -n : 0, -1]);
  return { total, notes: items.map((item) => unseal(chat, item)).reverse() };
}

export async function remove(chat, id) {
  const [items] = await db(["LRANGE", list(chat), 0, -1]);
  const item = items.find((it) => unseal(chat, it).id === id);
  if (!item) return { removed: false, total: items.length };
  const [, total] = await db(["LREM", list(chat), 1, item], ["LLEN", list(chat)]);
  return { removed: true, total };
}

export async function forget(chat) {
  await db(["DEL", list(chat)]);
  return { removed: true, total: 0 };
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
dream dreams dreamt dreamed dreaming tonight
а без более больше будет будто бы был была были было быть в вам вас ведь весь во вот впрочем все всегда всего всех всю
вы где да даже два для до другой его ее ей ему если есть еще ж же за зачем здесь и из или им ими их к как какая какой
какие какую когда кто куда ли лучше между меня мне много может можно мой моя мое мои мы на над надо наконец нас не него
нее ней нельзя нет ни нибудь никогда ним них ничего но ну о об один он она они оно опять от перед по под после потом
потому почти при про раз разве с сам свою себе себя сейчас со совсем так такой там тебе тебя тем теперь то тогда того
тоже только том тот три тут ты у уж уже хоть чего чем через что чтоб чтобы чуть эти этого этой этом этот эту это я
очень просто типа короче вообще кажется помню словно вроде какой-то что-то кто-то где-то как-то который которая которое
которые которых которую котором которой мной нам нами наш наша наше наши твой твоя твое твои свой своя свое свои сегодня
вчера завтра снова сначала затем сон сны сна сне сном снов снах снилось снился снилась снились приснилось приснился
приснилась приснились снится снятся сниться
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
  const r2 = regionAfter(whole, regionAfter(whole, 0));
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
    .map((e) => ({ word: [...e.forms].sort((a, b) => b[1] - a[1])[0][0], n: e.notes }));
}

const dayOf = (t) =>
  new Intl.DateTimeFormat("en-CA", { timeZone: TIMEZONE, year: "numeric", month: "2-digit", day: "2-digit" }).format(t);
const hourOf = (t) => Number(new Intl.DateTimeFormat("en-GB", { timeZone: TIMEZONE, hour: "2-digit", hourCycle: "h23" }).format(t));
const addDays = (day, n) => new Date(Date.parse(`${day}T00:00:00Z`) + n * DAY).toISOString().slice(0, 10);

export async function summary(chat, now = Date.now()) {
  const [items] = await db(["LRANGE", list(chat), 0, -1]);
  return stats(items.map((item) => unseal(chat, item)), now);
}

// Numbers for the page to put into words, in whatever language it is in.
export function stats(notes, now = Date.now()) {
  if (!notes.length) return { total: 0 };
  const out = {
    total: notes.length,
    first: notes[0].t,
    dreams: notes.filter((n) => n.k === "dream").length,
    thoughts: notes.filter((n) => n.k === "thought").length,
  };
  const recent = notes.filter((n) => n.t >= now - 30 * DAY);
  if (notes[0].t < now - 30 * DAY) out.month = { notes: recent.length, days: new Set(recent.map((n) => dayOf(n.t))).size };

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
  out.streak = { now: current, best };

  if (notes.length >= 5) {
    const hours = [0, 0, 0, 0];      // night, morning, afternoon, evening
    for (const n of notes) {
      const h = hourOf(n.t);
      hours[h < 5 ? 0 : h < 12 ? 1 : h < 17 ? 2 : 3]++;
    }
    const top = hours.indexOf(Math.max(...hours));
    if (hours[top] / notes.length >= 0.4) out.when = ["night", "morning", "afternoon", "evening"][top];
  }

  if (notes.length >= 3) {
    out.themes = {};
    for (const kind of ["dream", "thought"]) {
      const group = notes.filter((n) => n.k === kind);
      if (group.length >= 3) out.themes[kind] = themes(group);
    }
    if (!Object.keys(out.themes).length) out.themes.all = themes(notes);
  }

  // a note from a year, a month or a week ago (give or take a day)
  for (const ago of [365, 30, 7]) {
    const target = Date.parse(addDays(today, -ago));
    const old = notes.find((n) => Math.abs(Date.parse(dayOf(n.t)) - target) <= DAY);
    if (old) {
      out.ago = { days: ago, t: old.t, k: old.k, s: old.s.length > 280 ? `${old.s.slice(0, 280).trimEnd()}…` : old.s };
      break;
    }
  }
  return out;
}
