#!/usr/bin/env python3
"""Build today's edition: fetch the feeds in paper.toml, pick the stories, render docs/index.html.

Big news = stories covered by the most outlets. Headlines from different feeds are grouped
by shared keywords, and a group's size (distinct outlets, big newsrooms counting triple)
decides its rank. No AI involved.
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import copy
import html
import io
import json
import os
import random
import re
import shutil
import subprocess
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import segno
import trafilatura
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup
from PIL import Image

from translate import Translator

ROOT = Path(__file__).parent
SITE = ROOT / "docs"
BUILD = ROOT / "build"
UA = "Mozilla/5.0 (compatible; DailyPaper/1.0; +https://github.com)"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
GOOGLE_NEWS = "https://news.google.com/rss/search?hl=en-GB&gl=GB&ceid=GB:en&q="
NBK_RATES = "https://nationalbank.kz/rss/rates_all.xml"
CROSS_RATES = "https://open.er-api.com/v6/latest/"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

STOPWORDS = set("""
a an the and or but if of to in on at by for with from into onto over under about after before amid
against as is are was were be been being has have had do does did will would could should may might
can must not no this that these those it its he she they them his her their our we you i us
says say said told tells new news live latest update updates report reports reported watch video
photos pictures how why what who when where which more most than then also just still now first
last next year years day days week weeks month months today yesterday tomorrow one two three four
five six seven eight nine ten via per up down out off back get gets got make makes made take takes
took set sets amid while during across through make here there
""".split())

LIVE_SUFFIX = r"\s+[–—-]\s+([\w ]{0,30} live|live updates|latest updates?|as it happened)$"

# A coloured label on each card. A headline gets the topic whose words it uses most.
TOPICS = {
    "Conflict": "war strike missile troop military ceasefire attack drone army bomb invasion shelling hostage soldier",
    "Justice": "court trial sentenced jailed police murder arrest charged judge guilty fraud crime prison",
    "Politics": "election vote voter minister president parliament government senate congress mp labour "
                "tory conservative snp councillor campaign tokayev trump starmer democrat republican mazhilis policy law",
    "Business": "market stock share economy inflation bank rate company profit price tax budget investment trade "
                "tariff oil gdp job wage pension business tenge",
    "Tech": "ai tech nvidia apple google chip software cyber hack hacking robot spacex digital",
    "Sport": "football match cup league olympic tennis wta goal coach rugby boxing champion club medal",
    "Culture": "film music festival art book theatre exhibition concert museum gig album christmas hogmanay",
    "Health": "health nhs hospital doctor cancer virus vaccine disease medical patient",
    "Climate": "climate storm flood emission cop31 energy heat wildfire environment caspian",
}


# ---------------------------------------------------------------- data


@dataclass
class Story:
    title: str
    link: str
    source: str
    published: datetime | None
    summary: str
    image: str | None
    slot: str                     # which section/desk's feed it came from
    order: int                    # feed priority, lower = preferred
    position: int                 # place in its feed; outlets list their main stories first
    ignore: frozenset[str] = frozenset()   # a search feed's own query words
    live: bool = False            # a live blog
    kind: str | None = None       # "opinion", "analysis" or "explainer"
    tokens: frozenset[str] = frozenset()

    @property
    def via_google(self) -> bool:
        return "news.google.com" in self.link


@dataclass
class Cluster:
    stories: list[Story] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return list(dict.fromkeys(s.source for s in self.stories))

    @property
    def coverage(self) -> int:
        return len(self.sources)

    def majors(self, major: set[str]) -> int:
        return sum(1 for src in self.sources if src.lower() in major)

    def weight(self, major: set[str]) -> int:
        """Big newsrooms count triple; a pile of small sites (press releases, trade press) adds at most 2."""
        big = self.majors(major)
        return 3 * big + min(2, self.coverage - big)

    @property
    def position(self) -> int:
        return min(s.position for s in self.stories)

    @property
    def topic(self) -> set[str]:
        """Words shared by several of its headlines — what the story is about."""
        seen = Counter(t for ts in {s.tokens for s in self.stories} for t in ts)
        return {t for t, n in seen.items() if n >= 2}

    @property
    def newest(self) -> datetime:
        return max((s.published for s in self.stories if s.published), default=datetime.min.replace(tzinfo=timezone.utc))

    @property
    def span_hours(self) -> float:
        times = [s.published for s in self.stories if s.published]
        return (max(times) - min(times)).total_seconds() / 3600 if times else 0

    def pick(self, slot: str | None = None) -> Story:
        """Best story to print: from this slot if possible, from the outlet itself, with a picture and a snippet."""
        pool = [s for s in self.stories if slot is None or s.slot == slot] or self.stories
        return min(pool, key=lambda s: (s.via_google, not s.image, not s.summary, s.order))


# ---------------------------------------------------------------- fetching


def fetch(url: str, limit: int | None = None, agent: str = UA, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit) if limit else r.read()


def feed_url(spec: dict) -> str:
    if "google" in spec:
        return GOOGLE_NEWS + urllib.parse.quote_plus(spec["google"])
    return spec["url"]


def clean_text(text: str, limit: int = 240) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    text = re.sub(r"\s*The post .{0,300} appeared first on .*$", "", text)   # WordPress footer
    text = re.sub(r"^[^.]{0,60} provides news and information from [^.]*\.\s*", "", text)  # Astana Times header
    text = re.sub(r"\s*(\[…\]|\[\.\.\.\]|Continue reading\.*)$", "…", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:—–-") + "…"
    return text


def image_of(entry) -> str | None:
    found = []
    for m in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if m.get("url") and not str(m.get("type", "")).startswith("video") and m.get("medium") != "video":
            found.append((int(m.get("width") or 0), m["url"]))
    for link in entry.get("links", []):
        if str(link.get("type", "")).startswith("image"):
            found.append((0, link["href"]))
    if not found:   # WordPress and NPR put the picture inside the article text
        blob = " ".join(c.get("value", "") for c in entry.get("content", [])) + entry.get("summary", "")
        for tag in re.findall(r"<img\b[^>]*>", blob):
            src = re.search(r"src=[\"']([^\"']+)", tag)
            if src and not src.group(1).startswith("data:") and not re.search(r"width=[\"']?1[\"'\s>]|pixel", tag):
                found.append((0, html.unescape(src.group(1))))
                break
    if not found:
        return None
    url = max(found)[1]
    # BBC serves 240px thumbnails in RSS; the same image exists at 800px.
    return re.sub(r"(ichef\.bbci\.co\.uk/(?:ace|news)/(?:standard|ws)/)\d+/", r"\g<1>800/", url)


OG_IMAGE = re.compile(r"<meta\b[^>]*?(?:property|name)=[\"'](?:og:image|twitter:image)(?::src)?[\"'][^>]*>", re.I)


_pages: dict[str, str | None] = {}


def article_page(url: str) -> str | None:
    """An article's HTML, fetched once per run (pictures and the print edition both need it)."""
    if url not in _pages:
        try:
            _pages[url] = fetch(url, limit=2_000_000, agent=BROWSER_UA, timeout=12).decode("utf-8", "ignore")
        except Exception:
            _pages[url] = None
    return _pages[url]


def page_image(url: str) -> str | None:
    """The picture an article shows when shared (og:image), for feeds that don't include one."""
    page = article_page(url)
    if not page:
        return None
    for tag in OG_IMAGE.findall(page):
        content = re.search(r"content=[\"']([^\"']+)", tag)
        if content:
            return urllib.parse.urljoin(url, html.unescape(content.group(1).strip()))
    return None


FOREIGN = set("de la le les des du est une pour dans sur avec qui que der die das und ist mit für von zu el los las por para con una".split())


def looks_english(title: str) -> bool:
    if re.search(r"[Ѐ-ӿ؀-ۿ぀-ヿ一-鿿가-힯]", title):
        return False
    return len(FOREIGN & set(title.lower().split())) < 2


def kind_of(title: str, link: str) -> tuple[str, str | None]:
    """Strip "Opinion:"-style labels from a headline and return them as the story's kind."""
    if m := re.match(r"(opinion|comment|analysis)\s*[:|]\s*", title, re.I):
        return title[m.end():], "analysis" if m.group(1).lower() == "analysis" else "opinion"
    if "/commentisfree/" in link:
        return title.rsplit(" | ", 1)[0], "opinion"         # Guardian: "Headline | Columnist"
    if re.match(r"(what|why|how|who)\b", title, re.I) and title.endswith("?"):
        return title, "explainer"
    return title, None


def parse_feed(raw: bytes, spec: dict, slot: str, order: int, cutoff: datetime) -> list[Story]:
    stories = []
    is_google = "google" in spec
    ignore = frozenset(tokenize(spec["google"])) if is_google else frozenset()
    for position, e in enumerate(feedparser.parse(raw).entries):
        title = clean_text(e.get("title", ""), 300)
        title = re.sub(r"^[^\w‘'\"“(]+", "", title)                                  # "🔴 Breaking…"
        link = e.get("link")
        if not title or not link or not looks_english(title) or re.search(r"^(sponsored|advertisement|paid post|competition:|win\b)|\bnow £\d|\bvouchers?\b|\bdiscount\b", title, re.I):
            continue
        parsed = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None
        if published and published < cutoff:
            continue
        if is_google:
            source = (e.get("source") or {}).get("title") or "Google News"
            source = re.split(r"\s+[–|]\s+|\s+-\s+", source)[0]          # "The Diplomat – Asia-Pacific …"
            if " - " in title:
                title = title.rsplit(" - ", 1)[0]
            summary = ""           # Google News descriptions only repeat the headline
        else:
            source = spec["source"]
            summary = clean_text(e.get("summary", ""))
            if summary.rstrip("…").lower() in title.lower():
                summary = ""
        live = "/live/" in link or bool(re.search(LIVE_SUFFIX, title, re.I))
        title = re.sub(LIVE_SUFFIX, "", title, flags=re.I)
        title, kind = kind_of(title, link)
        stories.append(Story(title, link, source, published, summary, image_of(e), slot, order, position,
                             ignore, live, kind))
    return stories


def feed_specs(cfg: dict):
    """Yield (slot, (section, desk), feed spec) for every feed; a slot is a section or one desk of it."""
    for section in cfg["sections"]:
        for desk in section.get("desks") or [section]:
            slot = section["id"] if desk is section else f'{section["id"]}/{desk["title"]}'
            for spec in desk["feeds"]:
                yield slot, (section, desk), spec


def collect(cfg: dict, now: datetime) -> tuple[list[Story], list[tuple[str, str, int]]]:
    """Fetch every feed once. Returns all stories and the list of slots (id, title, limit)."""
    default_age = cfg["paper"].get("max_age_hours", 30)
    jobs = []        # (url, spec, slot, cutoff)
    slots = []
    for slot, (section, desk), spec in feed_specs(cfg):
        if not slots or slots[-1][0] != slot:
            slots.append((slot, desk["title"], desk.get("limit", 6)))
        age = desk.get("max_age_hours", section.get("max_age_hours", default_age))
        jobs.append((feed_url(spec), spec, slot, now - timedelta(hours=age)))

    urls = list(dict.fromkeys(j[0] for j in jobs))
    raw: dict[str, bytes] = {}
    with cf.ThreadPoolExecutor(12) as pool:
        futures = {pool.submit(fetch, u): u for u in urls}
        for fut in cf.as_completed(futures):
            u = futures[fut]
            try:
                raw[u] = fut.result()
            except Exception as ex:      # one dead feed must not stop the paper
                print(f"  ! feed failed: {u[:90]} — {ex}")

    stories = []
    for order, (url, spec, slot, cutoff) in enumerate(jobs):
        if url in raw:
            found = parse_feed(raw[url], spec, slot, order, cutoff)
            print(f"  {len(found):3} stories  {slot:32} {spec.get('source') or 'Google: ' + spec['google'][:40]}")
            stories += found
    return stories, slots


# ---------------------------------------------------------------- grouping & ranking


def stem(w: str) -> str:
    """Crude, but enough to match "denies/deny", "spying/spy", "launched/launch", "talks/talk"."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokenize(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", title.lower().replace("’", "'"))
    out = set()
    for w in words:
        w = re.sub(r"'s$", "", w)
        if len(w) < 3 or w in STOPWORDS:
            continue
        out.add(stem(w))
    return out


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    shared = len(a & b)
    if shared >= 3:
        return True
    return shared == 2 and shared / max(1, min(len(a), len(b))) >= 0.67


def cluster(stories: list[Story]) -> list[Cluster]:
    token_sets = [tokenize(s.title) for s in stories]
    # Words that say nothing about which event a headline is about don't count as a match:
    # a search feed's own query ("Kazakhstan", "European Commission") and words found
    # in a large share of all headlines.
    df = Counter(t for ts in token_sets for t in ts)
    common = {t for t, n in df.items() if n > max(10, len(stories) * 0.1)}
    for s, ts in zip(stories, token_sets):
        s.tokens = frozenset(ts - common - s.ignore)

    clusters: list[Cluster] = []
    by_link: dict[str, Cluster] = {}
    for s in sorted(stories, key=lambda s: s.order):
        home = by_link.get(s.link)
        if home is None and len(s.tokens) >= 2:
            home = next((c for c in clusters if any(similar(s.tokens, o.tokens) for o in c.stories)), None)
        if home is None:
            home = Cluster()
            clusters.append(home)
        home.stories.append(s)
        by_link[s.link] = home
    return clusters


def rank(clusters: list[Cluster], major: set[str]) -> list[Cluster]:
    return sorted(clusters, key=lambda c: (-c.weight(major), c.position, -c.newest.timestamp()))


def topic_of(s: Story) -> str | None:
    words = {stem(w) for w in re.findall(r"[a-z0-9]+", f"{s.title} {s.summary[:120]}".lower())}
    hits = {name: len(words & {stem(w) for w in vocab.split()}) for name, vocab in TOPICS.items()}
    best = max(hits, key=hits.get)
    return best if hits[best] else None


SOFT_TOPICS = {"Culture", "Sport", None}


def badges(c: Cluster, s: Story, now: datetime, major: set[str]) -> list[tuple[str, str]]:
    """Status labels: what kind of story this is and how fresh."""
    out = []
    age = (now - s.published).total_seconds() / 3600 if s.published else None
    if s.live:
        out.append(("live", "Live"))
    elif age is not None and age <= 3 and c.majors(major) >= 2 and topic_of(s) not in SOFT_TOPICS:
        out.append(("breaking", "Breaking"))
    elif c.coverage >= 3 and c.span_hours >= 12:
        out.append(("developing", "Developing"))
    elif age is not None and age <= 4:
        out.append(("new", "New"))
    if s.kind:
        out.append((s.kind, s.kind.title()))
    return out


def card(c: Cluster, now: datetime, major: set[str], slot: str | None = None) -> dict:
    s = c.pick(slot)
    return {
        "title": s.title, "link": s.link, "source": s.source, "published": s.published,
        "summary": s.summary, "image": s.image, "coverage": c.coverage,
        "others": [src for src in c.sources if src != s.source],
        "badges": badges(c, s, now, major), "topic": topic_of(s), "via_google": s.via_google,
    }


def lay_out(cfg: dict, stories: list[Story], slots: list[tuple[str, str, int]], now: datetime) -> tuple[list[dict], list[dict]]:
    major = {spec["source"].lower() for _, _, spec in feed_specs(cfg) if "source" in spec}
    clusters = rank(cluster(stories), major)
    used: set[int] = set()
    topics: list[set[str]] = []

    def fresh(c: Cluster, slot: str | None = None) -> bool:
        """Not printed yet, and not another angle on a story already printed."""
        words = c.pick(slot).tokens
        return id(c) not in used and not any(len(words & t) >= 2 for t in topics)

    def use(c: Cluster) -> None:
        used.add(id(c))
        if c.topic:
            topics.append(c.topic)

    # Front page = covered by at least two big national/international newsrooms;
    # local outlets (sections marked local) rank stories only inside their own section.
    national = {spec["source"].lower() for _, (section, _), spec in feed_specs(cfg)
                if "source" in spec and not section.get("local")}
    top = []
    for c in rank(clusters, national):
        if len(top) == cfg["paper"].get("top_stories", 5):
            break
        if c.majors(national) >= 2 and fresh(c):
            top.append(card(c, now, major))
            use(c)

    skip = {s["id"]: set(s.get("skip_topics", [])) for s in cfg["sections"]}
    filled: dict[str, list[dict]] = {}
    for slot, _title, limit in slots:
        picked, per_source = [], Counter()
        for c in clusters:
            if len(picked) == limit:
                break
            if not any(s.slot == slot for s in c.stories) or not fresh(c, slot):
                continue
            item = card(c, now, major, slot)
            if per_source[item["source"]] == 3 or item["topic"] in skip.get(slot.split("/")[0], ()):
                continue
            per_source[item["source"]] += 1
            picked.append(item)
            use(c)
        filled[slot] = picked

    sections = []
    for section in cfg["sections"]:
        entry = {"id": section["id"], "title": section["title"], "color": section.get("color", "#555")}
        if section.get("desks"):
            entry["desks"] = [{"title": d["title"], "flag": d.get("flag", ""), "stories": filled[f'{section["id"]}/{d["title"]}']}
                              for d in section["desks"]]
        else:
            entry["stories"] = filled[section["id"]]
        sections.append(entry)
    return top, sections


def add_pictures(cards: list[dict]) -> None:
    """Fill in missing pictures from the articles themselves. Google News links can't be followed."""
    missing = [c for c in cards if not c["image"] and not c["via_google"]]
    with cf.ThreadPoolExecutor(16) as pool:
        for c, img in zip(missing, pool.map(lambda c: page_image(c["link"]), missing)):
            c["image"] = img
    print(f"  pictures: {sum(1 for c in cards if c['image'])} of {len(cards)} stories "
          f"({sum(1 for c in missing if c['image'])} found on the article pages)")


# ---------------------------------------------------------------- weather, rates, quote, wish

WMO = {
    0: ("Clear", "clear"), 1: ("Mainly clear", "partly"), 2: ("Partly cloudy", "partly"), 3: ("Overcast", "cloudy"),
    45: ("Fog", "fog"), 48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
    80: ("Showers", "rain"), 81: ("Showers", "rain"), 82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm, hail", "storm"), 99: ("Thunderstorm, hail", "storm"),
}


def deg(t: float) -> str:
    n = round(t)
    return f"{'−' if n < 0 else ''}{abs(n)}°"


def forecast(place: dict) -> dict | None:
    query = urllib.parse.urlencode({
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": 4,
        "wind_speed_unit": "ms",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "wind_speed_10m_max,sunrise,sunset",
    })
    try:
        data = json.loads(fetch(f"{OPEN_METEO}?{query}"))
    except Exception as ex:
        print(f"  ! weather for {place['city']} failed — {ex}")
        return None
    cur, day = data["current"], data["daily"]
    label, kind = WMO.get(cur["weather_code"], ("—", "cloudy"))
    days = []
    for i in range(len(day["time"])):
        dl, dk = WMO.get(day["weather_code"][i], ("—", "cloudy"))
        days.append({
            "date": date.fromisoformat(day["time"][i]),
            "label": dl, "kind": dk, "high": day["temperature_2m_max"][i],
            "hi": deg(day["temperature_2m_max"][i]), "lo": deg(day["temperature_2m_min"][i]),
            "rain": day["precipitation_probability_max"][i] or 0, "wind": day["wind_speed_10m_max"][i] or 0,
        })
    return {
        "city": place["city"], "role": place.get("role", ""), "utc_offset": place.get("utc_offset"),
        "now": deg(cur["temperature_2m"]), "feels": deg(cur["apparent_temperature"]),
        "label": label, "kind": kind, "wind": round(cur["wind_speed_10m"]), "humidity": cur["relative_humidity_2m"],
        "sunrise": day["sunrise"][0][-5:], "sunset": day["sunset"][0][-5:], "days": days,
    }


def weather_tip(wx: dict | None, tips: dict) -> str | None:
    if not wx:
        return None
    d = wx["days"][0]
    if d["kind"] == "storm":
        return tips.get("storm")
    if d["kind"] == "snow":
        return tips.get("snow")
    if d["kind"] == "rain" or d["rain"] >= 60:
        return tips.get("rain")
    if d["wind"] >= 10:
        return tips.get("wind")
    if d["kind"] == "fog":
        return tips.get("fog")
    if d["high"] <= 3:
        return tips.get("cold")
    if d["kind"] in ("clear", "partly") and d["rain"] < 30:
        return tips.get("sun")
    return tips.get("mild")


CURRENCIES = {"USD": ("US dollar", "$"), "EUR": ("Euro", "€"), "GBP": ("Pound sterling", "£"),
              "RUB": ("Russian rouble", "₽"), "CNY": ("Chinese yuan", "¥"), "TRY": ("Turkish lira", "₺"),
              "AED": ("UAE dirham", "AED "), "KGS": ("Kyrgyz som", "KGS "), "UZS": ("Uzbek sum", "UZS "),
              "JPY": ("Japanese yen", "¥"), "CHF": ("Swiss franc", "CHF ")}


def rates(cfg: dict) -> dict | None:
    wanted = cfg.get("rates", {}).get("currencies")
    if not wanted:
        return None
    try:
        root = ET.fromstring(fetch(NBK_RATES))
    except Exception as ex:
        print(f"  ! exchange rates failed — {ex}")
        return None
    found, on = {}, None
    for item in root.iter("item"):
        code = (item.findtext("title") or "").strip()
        if code in wanted:
            change = (item.findtext("change") or "0").strip()
            name, symbol = CURRENCIES.get(code, (code, code + " "))
            found[code] = {
                "code": code, "name": name, "symbol": symbol,
                "per": int(item.findtext("quant") or 1),
                "value": float(item.findtext("description")),
                "change": change.replace("-", "−"),
                "dir": "up" if change.startswith("+") and float(change) else "down" if change.startswith("-") else "flat",
            }
            on = (item.findtext("pubDate") or "").strip()
    rows = [found[c] for c in wanted if c in found]
    if not rows:
        return None
    cross = {}     # what one of the main currency buys in dollars and euros
    try:
        data = json.loads(fetch(CROSS_RATES + rows[0]["code"]))
        cross = {k: data["rates"][k] for k in ("USD", "EUR") if k != rows[0]["code"] and k in data["rates"]}
    except Exception as ex:
        print(f"  ! cross rates failed — {ex}")
    return {"rows": rows, "on": on, "cross": [(CURRENCIES[k][1], v) for k, v in cross.items()]}


def of_the_day(items: list, today: date, salt: str):
    """Same pick all day, a different one tomorrow, in a shuffled order that repeats only after the whole list."""
    if not items:
        return None
    order = list(range(len(items)))
    random.Random(salt).shuffle(order)
    return items[order[today.toordinal() % len(items)]]


# ---------------------------------------------------------------- print edition text

# How much of each article the print edition carries, in words.
PRINT_WORDS = {"lead": 450, "top": 200, "feature": 220, "story": 130, "desk": 110}
JUNK = re.compile(r"(sign up|subscribe|newsletter|follow us|read more|click here|getty images|image source|"
                  r"image caption|copyright|all rights reserved|advertisement|share this|related:)", re.I)


def article_text(url: str, words: int) -> list[str]:
    """The first paragraphs of an article, up to about `words` words."""
    page = article_page(url)
    if not page:
        return []
    text = trafilatura.extract(page, favor_precision=True, include_comments=False, include_tables=False) or ""
    out, count = [], 0
    for para in text.split("\n"):
        para = para.strip()
        if len(para) < 60 or JUNK.search(para[:80]):
            continue
        out.append(para)
        count += len(para.split())
        if count >= words:
            break
    return out


def add_bodies(top: list[dict], sections: list[dict]) -> None:
    """Fetch article text for the stories the print edition runs in full. Google News links can't be followed."""
    jobs = [(c, PRINT_WORDS["lead" if i == 0 else "top"]) for i, c in enumerate(top)]
    for sec in sections:
        if sec.get("desks"):
            jobs += [(c, PRINT_WORDS["desk"]) for d in sec["desks"] for c in d["stories"][:3]]
        else:
            jobs += [(c, PRINT_WORDS["feature" if i == 0 else "story"]) for i, c in enumerate(sec["stories"][:5])]
    jobs = [(c, n) for c, n in jobs if not c["via_google"]]
    def same(a: str, b: str) -> bool:
        a, b = re.sub(r"\W+", " ", a).lower().strip(), re.sub(r"\W+", " ", b).lower().strip()
        return bool(a and b) and (a.startswith(b[:60]) or b.startswith(a[:60]))

    with cf.ThreadPoolExecutor(12) as pool:
        for (c, _), body in zip(jobs, pool.map(lambda job: article_text(job[0]["link"], job[1]), jobs)):
            # many pages open by repeating the headline or the summary the paper already prints
            c["body"] = [p for p in body if not same(p, c["title"]) and not same(p, c["summary"])]
    print(f"  print text: {sum(1 for c, _ in jobs if c.get('body'))} of {len(jobs)} articles")


# ---------------------------------------------------------------- languages


def strings(i18n: dict, lang: str):
    """t(key, **values): interface text in this edition's language (English when missing)."""
    own, en = i18n.get(lang, {}), i18n.get("en", {})

    def t(key: str, **values) -> str:
        text = own.get(key) or en.get(key) or key
        return text.format(**values) if values else text
    return t


def dates(i18n: dict, lang: str):
    own = i18n.get(lang, {})
    if "_months" not in own:
        return (lambda d: f"{d:%A}, {d.day} {d:%B %Y}"), (lambda d: f"{d:%a}, {d.day} {d:%b}"), (lambda d: f"{d:%a}")

    def long(d: date) -> str:
        return own["_date_long"].format(year=d.year, month=own["_months"][d.month - 1], day=d.day,
                                        weekday=own["_weekdays_long"][d.weekday()])

    def short(d: date) -> str:
        return own["_date_short"].format(month=own["_months"][d.month - 1], day=d.day)
    return long, short, (lambda d: own["_weekdays"][d.weekday()])


def translate_edition(top: list[dict], sections: list[dict], extras: dict, lang: str, tr: Translator):
    """Copies of the day's stories (and the quote and wish) in another language."""
    top, sections, extras = copy.deepcopy(top), copy.deepcopy(sections), copy.deepcopy(extras)
    cards = top + [s for sec in sections for d in sec.get("desks") or [sec] for s in d["stories"]]
    quote, wish = extras["quote"], extras["wish"]
    native = lang == "kk" and quote and quote.get("original")      # Kazakh proverbs need no translation
    texts = [x for c in cards for x in [c["title"], c["summary"], *c.get("body", [])]]
    texts += [wish.get("text"), wish.get("tip")] + ([] if native or not quote else [quote["text"]])
    done = tr.many([x for x in texts if x], lang)
    for c in cards:
        c["title"], c["summary"] = done.get(c["title"], c["title"]), done.get(c["summary"], c["summary"])
        c["body"] = [done.get(p, p) for p in c.get("body", [])]
    for k in ("text", "tip"):
        if wish.get(k):
            wish[k] = done.get(wish[k], wish[k])
    if quote:
        if native:
            quote["text"], quote["original"] = quote["original"], None
        else:
            quote["text"] = done.get(quote["text"], quote["text"])
    return top, sections, extras


# ---------------------------------------------------------------- output


def ago(dt: datetime | None, now: datetime, t) -> str:
    if not dt:
        return ""
    minutes = int((now - dt).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} {t('min ago')}"
    if minutes < 48 * 60:
        return f"{minutes // 60} {t('h ago')}"
    return f"{minutes // 1440} {t('days ago')}"


def editions(folder: Path, keep_days: int, today: date) -> list[date]:
    out = []
    for f in (folder / "archive").glob("*.html"):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if (today - d).days > keep_days:
            f.unlink()
        else:
            out.append(d)
    return sorted(out, reverse=True)


_shrunk: dict[tuple[str, int], str] = {}


def shrink(url: str | None, width: int = 1100) -> str | None:
    """A print-sized copy of a picture, embedded in the page. Full-size news photos make a 50 MB PDF."""
    if not url:
        return url
    if (url, width) not in _shrunk:
        try:
            img = Image.open(io.BytesIO(fetch(url, agent=BROWSER_UA, timeout=15))).convert("RGB")
            img.thumbnail((width, width * 2))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=72, optimize=True, progressive=True)
            _shrunk[url, width] = "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
        except Exception:
            _shrunk[url, width] = url
    return _shrunk[url, width]


def qr(url: str) -> Markup:
    return Markup(segno.make(url, error="m").svg_inline(omitsize=True, border=0, dark="#111"))


def short_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    path = parts.path.rstrip("/")
    return parts.netloc.removeprefix("www.") + (path if len(path) < 28 else path[:26] + "…")


def to_pdf(page: Path, pdf: Path) -> bool:
    chrome = (os.environ.get("CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
              or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    flags = ["--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=25000"]
    if os.environ.get("CI"):
        flags.append("--no-sandbox")
    try:
        subprocess.run([chrome, *flags, f"--print-to-pdf={pdf}", page.as_uri()], check=True, timeout=180,
                       capture_output=True)
        return pdf.exists()
    except Exception as ex:
        print(f"  ! PDF failed — {ex}")
        return False


def main() -> None:
    cfg = tomllib.loads((ROOT / "paper.toml").read_text())
    quotes = tomllib.loads((ROOT / "quotes.toml").read_text()).get("quote", [])
    wishes = tomllib.loads((ROOT / "wishes.toml").read_text())
    i18n = tomllib.loads((ROOT / "i18n.toml").read_text())
    tz = ZoneInfo(cfg["paper"].get("timezone", "UTC"))
    now = datetime.now(timezone.utc)
    today = now.astimezone(tz).date()
    languages = cfg["paper"].get("languages", ["en"])
    print_languages = cfg.get("print", {}).get("languages", [])

    print("Fetching feeds…")
    stories, slots = collect(cfg, now)
    top, sections = lay_out(cfg, stories, slots, now)
    cards = top + [s for sec in sections for d in sec.get("desks") or [sec] for s in d["stories"]]
    add_pictures(cards)
    if print_languages:
        add_bodies(top, sections)

    places = [wx for wx in (forecast(p) for p in cfg.get("places", [])) if wx]
    fx = rates(cfg)
    reader = cfg.get("reader", {})
    extras = {
        "quote": of_the_day(quotes, today, "quotes"),
        "wish": {"text": of_the_day(wishes.get("wishes", []), today, "wishes"),
                 "tip": weather_tip(places[0] if places else None, wishes.get("tips", {}))},
    }
    all_sources = sorted({s.source for s in stories}, key=str.lower)
    number = (today - cfg["paper"]["founded"]).days + 1
    built = now.astimezone(tz).strftime("%H:%M")
    home_time, home_diff = "", None       # home clock as of building; the page's script keeps it ticking
    if len(places) > 1 and places[1]["utc_offset"] is not None:
        home_time = (now + timedelta(hours=places[1]["utc_offset"])).strftime("%H:%M")
        home_diff = places[1]["utc_offset"] - now.astimezone(tz).utcoffset().total_seconds() / 3600

    env = Environment(loader=FileSystemLoader(ROOT), autoescape=True, trim_blocks=True, lstrip_blocks=True)
    env.filters.update(qr=qr, short_url=short_url, shrink=shrink)
    translator = Translator(ROOT / "cache" / "translations.json", today)
    switcher = [(code, i18n.get(code, {}).get("_name", "English")) for code in languages]
    BUILD.mkdir(exist_ok=True)
    pdfs = {}

    for lang in languages:
        if lang == "en":
            l_top, l_sections, l_extras = top, sections, extras
        else:
            print(f"Translating into {lang}…")
            l_top, l_sections, l_extras = translate_edition(top, sections, extras, lang, translator)
        t = strings(i18n, lang)
        long_date, short_date, weekday = dates(i18n, lang)
        folder = SITE if lang == "en" else SITE / lang
        (folder / "archive").mkdir(parents=True, exist_ok=True)
        (folder / "archive" / f"{today}.html").touch()
        past = [d for d in editions(folder, cfg["paper"].get("archive_days", 60), today) if d != today]
        context = dict(
            paper=cfg["paper"], reader=reader, reader_name=i18n.get(lang, {}).get("_reader", reader.get("name")),
            lang=lang, html_lang=i18n.get(lang, {}).get("_html", lang), switcher=switcher,
            edition={"date": today.isoformat(), "long": long_date(today), "short": short_date(today),
                     "number": number, "built": built},
            quote=l_extras["quote"], wish=l_extras["wish"], places=places, home_time=home_time, home_diff=home_diff,
            rates=fx, top=l_top, sections=l_sections, past=past[:14], sources=all_sources, long_date=long_date,
            t=t, weekday=weekday, ago=lambda dt, t=t: ago(dt, now, t),
        )
        root = "" if lang == "en" else "../"
        page = env.get_template("template.html")
        (folder / "index.html").write_text(page.render(**context, root=root, home=""))
        (folder / "archive" / f"{today}.html").write_text(page.render(**context, root=root + "../", home="../"))
        if lang in print_languages:
            sheet = BUILD / f"paper-{lang}.html"
            sheet.write_text(env.get_template("print.html").render(**context))
            if to_pdf(sheet, BUILD / f"paper-{lang}.pdf"):
                pdfs[lang] = str(BUILD / f"paper-{lang}.pdf")
    translator.save()

    lead = fx["rows"][0] if fx else None
    summary = {
        "paper": cfg["paper"]["name"], "date": dates(i18n, "en")[0](today), "number": number,
        "reader": reader.get("name"), "greeting": reader.get("greeting"), "signoff": reader.get("signoff"),
        **extras,
        "weather": [f'{w["city"]} {w["now"]}, {w["label"].lower()} (today {w["days"][0]["lo"]}…{w["days"][0]["hi"]})' for w in places],
        "rates": lead and f'{lead["symbol"]}1 = {lead["value"]:.2f} ₸',
        "top": [{"title": c["title"], "link": c["link"], "source": c["source"], "coverage": c["coverage"]} for c in top],
        "pdfs": {lang: pdfs[lang] for lang in cfg.get("print", {}).get("send", []) if lang in pdfs},
    }
    (BUILD / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    print(f"Edition No. {number}: {len(top)} top stories, {len(cards) - len(top)} in sections, from {len(stories)} "
          f"headlines by {len(all_sources)} outlets · languages: {', '.join(languages)} · PDFs: {', '.join(pdfs) or 'none'}")


if __name__ == "__main__":
    main()
