#!/usr/bin/env python3
"""Send today's edition to Telegram. Runs after build.py, once the page is online.

Needs two environment variables: TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID.
To find your chat id: message your bot once, then run  TELEGRAM_BOT_TOKEN=... python notify.py --chat-id
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SUMMARY = Path(__file__).parent / "build" / "kk" / "summary.json"
JOURNAL_LINE = "Write down a dream or a thought →"


def telegram(token: str, method: str, payload: dict | None = None, *, body: bytes | None = None,
             content_type: str = "application/json") -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body if body is not None else json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as err:
        sys.exit(f"Telegram said no ({err.code}): {err.read().decode()}")


def send_pdf(token: str, chat_id: str, path: Path, filename: str, caption: str) -> None:
    boundary = uuid.uuid4().hex
    fields = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in (("chat_id", chat_id), ("caption", caption))
    )
    file = (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: application/pdf\r\n\r\n").encode() + path.read_bytes() + b"\r\n"
    telegram(token, "sendDocument", body=fields + file + f"--{boundary}--\r\n".encode(),
             content_type=f"multipart/form-data; boundary={boundary}")


def show_chat_ids(token: str) -> None:
    chats = {}
    for update in telegram(token, "getUpdates")["result"]:
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("No messages yet — send any message to your bot in Telegram, then run this again.")
    for chat_id, name in chats.items():
        print(f"chat id: {chat_id}   ({name})")


def journal_link(chat_id: str) -> str | None:
    """A one-time link that connects the reader's browser to her journal (bot/api/_journal.js checks it).

    It goes after # so it never reaches GitHub Pages or any server log; the page sends it to the bot once.
    """
    secret = os.environ.get("JOURNAL_LINK_SECRET")
    if not secret:
        return None
    until = int(time.time()) + 2 * 86400
    sig = hmac.new(secret.encode(), f"claim.{chat_id}.{until}".encode(), hashlib.sha256).digest()
    return f"{chat_id}.{until}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def message(s: dict, url: str | None, journal: str | None = None) -> str:
    """The morning message. With a journal link, the paper links are personal and end with the journal."""
    e = html.escape
    own = f"#t={journal}" if journal else ""
    labels = s.get("labels") or {}
    lines = [f"<b>🗞 {e(s['paper'])}</b> · {e(labels.get('No.', 'No.'))} {s['number']}", f"<i>{e(s['date'])}</i>", ""]
    if s.get("reader"):
        lines.append(f"☀️ <b>{e(s['greeting'] + ', ' if s.get('greeting') else '')}{e(s['reader'])}!</b>")
    if q := s.get("quote"):
        lines += [f"<i>“{e(q['text'].replace(' / ', ' '))}”</i> — {e(q['author'])}", ""]
    for line in s.get("weather") or []:
        lines.append(f"🌡 {e(line)}")
    if s.get("rates"):
        lines.append(f"💱 {e(s['rates'])}")
    if s["top"]:
        lines += ["", f"<b>{e(labels.get('Top stories', 'Top stories'))}</b>"]
        for i, t in enumerate(s["top"], 1):
            lines.append(f'{i}. <a href="{e(t["link"], quote=True)}">{e(t["title"])}</a> — {e(t["source"])}')
    if url:
        lines += ["", f'<a href="{e(url + own, quote=True)}">{e(labels.get("Read today’s paper →", "Read today’s paper →"))}</a>']
        if "zh" in (s.get("pdfs") or {}):
            lines.append(f'<a href="{e(url + "zh/" + own, quote=True)}">中文版 →</a>')
    wish = s.get("wish") or {}
    if wish.get("text"):
        lines += ["", f"💛 {e(wish['text'])}"]
        if wish.get("tip"):
            lines.append(f"<i>{e(wish['tip'])}</i>")
        if s.get("signoff"):
            lines.append(e(s["signoff"]))
    if url and journal:
        line = labels.get(JOURNAL_LINE, JOURNAL_LINE)
        lines += ["", f'🌙 <a href="{e(url + own + "&go=journal", quote=True)}">{e(line)}</a>']
    return "\n".join(lines)


def caption(s: dict, url: str | None, limit: int = 1024) -> str:
    """The message cut down to fit under a PDF: Telegram allows 1024 characters there."""
    e = html.escape
    labels = s.get("labels") or {}
    head = [f"<b>🗞 {e(s['paper'])}</b> · {e(labels.get('No.', 'No.'))} {s['number']}", f"<i>{e(s['date'])}</i>", ""]
    info = [f"🌡 {e(w)}" for w in s.get("weather") or []] + ([f"💱 {e(s['rates'])}"] if s.get("rates") else [])
    tail = [f'<a href="{e(url, quote=True)}">{e(labels.get("Read today’s paper →", "Read today’s paper →"))}</a>'] if url else []
    for n in range(len(s["top"]), -1, -1):          # drop headlines from the end until it fits
        tops = [f'{i}. <a href="{e(t["link"], quote=True)}">{e(t["title"])}</a>' for i, t in enumerate(s["top"][:n], 1)]
        lines = head + info + (["", f"<b>{e(labels.get('Top stories', 'Top stories'))}</b>", *tops] if tops else []) + [""] + tail
        text = "\n".join(lines)
        if len(html.unescape(re.sub(r"<[^>]+>", "", text))) <= limit:
            return text
    return "\n".join(head + tail)


def main() -> None:
    if "--journal-link" in sys.argv:          # a one-time journal link for one reader, e.g. to try it out
        chat_id = sys.argv[sys.argv.index("--journal-link") + 1]
        link = journal_link(chat_id)
        print(f"https://takky19ima.github.io/morning-edition/#t={link}&go=journal" if link else "JOURNAL_LINK_SECRET is not set")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set — skipping Telegram.")
        return
    if "--chat-id" in sys.argv:
        show_chat_ids(token)
        return
    if "--set-webhook" in sys.argv:
        url = sys.argv[sys.argv.index("--set-webhook") + 1]
        telegram(token, "setWebhook", {"url": url, "allowed_updates": ["message"]})
        today = {"command": "today", "description": "Today’s paper"}
        telegram(token, "setMyCommands", {"commands": [today]})
        # /my is listed only in its owner's chat (it still works for anyone who types it)
        for owner in filter(None, os.environ.get("MY_CHAT_ID", "").split(",")):
            telegram(token, "setMyCommands", {"commands": [today, {"command": "my", "description": "Мой выпуск"}],
                                              "scope": {"type": "chat", "chat_id": owner.strip()}})
        print(f"Bot now answers through {url}")
        return
    # one or more readers, comma-separated
    readers = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not readers:
        print("TELEGRAM_CHAT_ID is not set — skipping Telegram.")
        return

    path = Path(sys.argv[sys.argv.index("--summary") + 1]) if "--summary" in sys.argv else SUMMARY
    summary = json.loads(path.read_text())
    url = summary.get("url") or os.environ.get("PAGE_URL")
    text = summary.get("message") or message(summary, url)
    caption = (summary.get("labels") or {}).get("Today’s paper to print", "Today’s paper to print")
    slug = "-".join(summary["paper"].split())
    preview = {"url": url, "prefer_large_media": True} if url else {"is_disabled": True}
    names = {"en": "English", "kk": "Қазақша", "zh": "中文", "ru": "Русский"}
    stamp = summary.get("day", "today")
    for i, chat_id in enumerate(readers, 1):
        # papers with a journal give each reader their own links (see journal_link)
        link = summary.get("journal") and journal_link(chat_id)
        telegram(token, "sendMessage", {
            "chat_id": chat_id, "text": message(summary, url, link) if link else text,
            "parse_mode": "HTML", "link_preview_options": preview,
        })
        for lang, pdf in (summary.get("pdfs") or {}).items():
            send_pdf(token, chat_id, Path(pdf), f"{slug}-{stamp}-{lang}.pdf", f"🖨 {caption} · {names.get(lang, lang)}")
        print(f"Sent the paper and {len(summary.get('pdfs') or {})} PDF(s) to reader {i} of {len(readers)}.")


if __name__ == "__main__":
    main()
