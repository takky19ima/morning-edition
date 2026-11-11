# The Morning Edition + «Утро»

Two automatic morning papers from one codebase:

| Paper | For | Settings | Website | Bot |
|---|---|---|---|---|
| The Morning Edition | KK in Edinburgh (EN · ҚАЗ · 中文) | `papers/kk.toml` | `/` | /start, /today |
| «Утро» | Astana, in Russian, a modern magazine | `papers/my.toml` | `/my/` | /my |

Build one: `python build.py papers/kk.toml` or `python build.py papers/my.toml`.
«Утро» uses `magazine.html` and `magazine-print.html`. It is built every morning but not sent: it comes only with /my
(a manual run with "Send to Telegram" sends it to the `TELEGRAM_CHAT_ID_MY` readers).

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

### Her journal

At the end of KK's web page there is a page for her dreams and thoughts. Only a browser her morning
Telegram message has connected sees it: each reader's message carries a one-time link after `#t=`
(signed by `notify.py` with `JOURNAL_LINK_SECRET`), which the page trades for a key it keeps.
A forwarded message's link has already been used, so it opens nothing. She writes, deletes, downloads
everything as a text file, and "Look back" counts it up (no AI: totals, days in a row, words that keep
coming back, a note from a week, month or year ago).

The notes are encrypted (AES-256-GCM) in `bot/api/_journal.js` and stored in Upstash Redis, a Vercel
Marketplace integration. They never go to this repo, the website or any log.

### Alarm clock

GitHub sometimes skips or delays scheduled runs. Vercel's cron (`bot/vercel.json`) calls `bot/api/alarm.js`
once a morning per paper, which starts the workflow too; `done.py` makes sure a paper goes out only once.

| Vercel environment variable | What |
|---|---|
| `KV_REST_API_URL`, `KV_REST_API_TOKEN` | the journal's database, added by the Upstash integration |
| `JOURNAL_KEY` | the encryption key (random); lose it and the notes can't be read |
| `JOURNAL_LINK_SECRET` | also a GitHub secret; signs the morning links |
| `JOURNAL_TZ` | optional, her time zone for streaks (default Europe/London) |
| `CRON_SECRET` | Vercel sends it with each cron call, so only the cron can ring the alarm |
| `ALARM_GITHUB_TOKEN` | fine-grained GitHub token for this repo only, Actions: read and write |

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
