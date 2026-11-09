#!/usr/bin/env python3
"""Send today's edition to Telegram. Runs after build.py, once the page is online.

Needs two environment variables: TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID.
To find your chat id: message your bot once, then run  TELEGRAM_BOT_TOKEN=... python notify.py --chat-id
"""
import html
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SUMMARY = Path(__file__).parent / "build" / "summary.json"


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


def message(s: dict, url: str | None) -> str:
    e = html.escape
    lines = [f"<b>🗞 {e(s['paper'])}</b> · No. {s['number']}", f"<i>{e(s['date'])}</i>", ""]
    if s.get("reader"):
        lines.append(f"☀️ <b>{e(s['greeting'] + ', ' if s.get('greeting') else '')}{e(s['reader'])}!</b>")
    if q := s.get("quote"):
        lines += [f"<i>“{e(q['text'].replace(' / ', ' '))}”</i> — {e(q['author'])}", ""]
    for line in s.get("weather") or []:
        lines.append(f"🌡 {e(line)}")
    if s.get("rates"):
        lines.append(f"💱 {e(s['rates'])}")
    if s["top"]:
        lines += ["", "<b>Top stories</b>"]
        for i, t in enumerate(s["top"], 1):
            lines.append(f'{i}. <a href="{e(t["link"], quote=True)}">{e(t["title"])}</a> — {e(t["source"])}')
    if url:
        lines += ["", f'<a href="{e(url, quote=True)}">Read today’s paper →</a>']
    wish = s.get("wish") or {}
    if wish.get("text"):
        lines += ["", f"💛 {e(wish['text'])}"]
        if wish.get("tip"):
            lines.append(f"<i>{e(wish['tip'])}</i>")
        if s.get("signoff"):
            lines.append(e(s["signoff"]))
    return "\n".join(lines)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set — skipping Telegram.")
        return
    if "--chat-id" in sys.argv:
        show_chat_ids(token)
        return
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        print("TELEGRAM_CHAT_ID is not set — skipping Telegram.")
        return

    summary = json.loads(SUMMARY.read_text())
    url = os.environ.get("PAGE_URL")
    preview = {"url": url, "prefer_large_media": True} if url else {"is_disabled": True}
    telegram(token, "sendMessage", {
        "chat_id": chat_id, "text": message(summary, url), "parse_mode": "HTML", "link_preview_options": preview,
    })
    print(f"Sent to Telegram chat {chat_id}.")
    names = {"en": "English", "kk": "Қазақша", "zh": "中文"}
    for lang, pdf in (summary.get("pdfs") or {}).items():
        stamp = summary["date"].split(", ", 1)[-1].replace(" ", "-")
        send_pdf(token, chat_id, Path(pdf), f"Morning-Edition-{stamp}-{lang}.pdf",
                 f"🖨 Today’s paper to print · {names.get(lang, lang)}")
        print(f"Sent the {lang} PDF.")


if __name__ == "__main__":
    main()
