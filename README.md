# The Morning Edition + «Утро»

Two automatic morning papers from one codebase:

| Paper | For | Settings | Website | Bot |
|---|---|---|---|---|
| The Morning Edition | KK in Edinburgh (EN · ҚАЗ · 中文) | `papers/kk.toml` | `/` | /start, /today |
| «Утро» | Astana, in Russian, a modern magazine | `papers/my.toml` | `/my/` | /my |

Build one: `python build.py papers/kk.toml` or `python build.py papers/my.toml`.
«Утро» uses `magazine.html` and `magazine-print.html`; its readers are in the `TELEGRAM_CHAT_ID_MY` secret.

---


A newspaper for KK that writes itself every morning: Edinburgh and Scotland, what's on,
Kazakhstan, the world, economy and new laws, weather here and at home, the tenge rate, a quote
of the day and a good-day wish. In English, Kazakh and Chinese, on the web and as a printable PDF.
Built from free public feeds, published on GitHub Pages, sent to Telegram.

## How it works

1. `build.py` reads the feeds in `paper.toml` (outlets' RSS feeds and Google News searches).
2. Headlines about the same event are grouped. A story ranks higher the more newsrooms cover it,
   with big outlets counting more. The top 5 make the front page.
3. Each story gets a picture (from the feed or the article page), a topic and a status label
   (Live, Breaking, Developing, New, Opinion, Explainer).
4. `translate.py` machine-translates the stories into Kazakh and Chinese (Google Translate's free
   web endpoint, cached in `cache/`). Interface wording is written by hand in `i18n.toml`.
5. `template.html` makes the web pages: `docs/index.html`, `docs/kk/`, `docs/zh/`, plus dated copies in `archive/`.
6. `print.html` makes an A4 paper with longer excerpts and QR codes, turned into PDFs by Chrome.
   The PDFs only go to Telegram; the articles' text is not published on the website.
7. `notify.py` sends the top stories, the link and the PDF to Telegram.
8. `.github/workflows/daily.yml` runs it all every morning (about 06:00 in Edinburgh).

## The bot

`bot/` is a tiny Vercel function (https://bot-woad-psi-33.vercel.app/api/telegram). When someone sends
/start or /today to the bot, it replies with today's morning message, read from `docs/telegram.json`.
It needs no bot token: the reply goes back inside Telegram's own request. Vercel redeploys it on every push.

Readers who get the paper every morning are listed in the `TELEGRAM_CHAT_ID` secret, comma-separated.
A new reader's chat id shows up in the Vercel logs (`chat 123456789 (Name) sent /start`) after they press Start.
If the bot's address ever changes: Actions → **Connect bot** → Run workflow with the new URL.

## Change things

| What | Where |
|---|---|
| Name, city, languages, sections, feeds, which PDF goes to Telegram | `papers/kk.toml`, `papers/my.toml` |
| Quotes of the day | `quotes.toml` |
| Good-day wishes and weather tips | `wishes.toml` |
| Kazakh and Chinese interface wording | `i18n.toml` |

For an extra edition: GitHub → **Actions** → **Daily paper** → **Run workflow**.

## Run it on your Mac

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python build.py papers/kk.toml && open docs/index.html build/kk/paper-en.pdf
```

## Setup (once)

1. Make the repository **public** (GitHub Pages and Actions are free for public repos).
2. **Settings → Pages → Source: GitHub Actions.**
3. **Settings → Secrets and variables → Actions**: add `TELEGRAM_BOT_TOKEN` (from @BotFather) and
   `TELEGRAM_CHAT_ID` (message the bot, then run `TELEGRAM_BOT_TOKEN=... python notify.py --chat-id`).
