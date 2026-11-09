#!/bin/bash
# One-time Telegram setup: saves the bot token and your chat id as GitHub secrets
# and sends a test message. Run it in Terminal from this folder after the repo exists:
#   ./setup_telegram.sh
set -euo pipefail
cd "$(dirname "$0")"

read -rsp "Paste your bot token from @BotFather (it stays hidden): " TOKEN
echo
CHAT=$(TELEGRAM_BOT_TOKEN="$TOKEN" python3 notify.py --chat-id | awk '/chat id:/ {print $3; exit}')
if [ -z "$CHAT" ]; then
  echo "No chat found. Open your bot in Telegram, press Start, send it any message, then run this again."
  exit 1
fi

printf %s "$TOKEN" | gh secret set TELEGRAM_BOT_TOKEN
printf %s "$CHAT" | gh secret set TELEGRAM_CHAT_ID

TELEGRAM_BOT_TOKEN="$TOKEN" TELEGRAM_CHAT_ID="$CHAT" python3 - <<'EOF'
import os, notify
notify.telegram(os.environ["TELEGRAM_BOT_TOKEN"], "sendMessage", {
    "chat_id": os.environ["TELEGRAM_CHAT_ID"],
    "text": "✅ The Morning Edition is connected. The paper will arrive here every morning.",
})
EOF
echo "Done — secrets saved, and a test message is in your Telegram."
