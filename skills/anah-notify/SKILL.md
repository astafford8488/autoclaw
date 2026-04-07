---
name: anah-notify
description: "ANAH Discord notification dispatcher — sends alerts, heartbeat summaries, and status updates to Discord via webhooks. Triggers on phrases like 'send notification', 'discord alert', 'notify discord', 'test notifications'."
---

# ANAH Notify — Discord Webhook Dispatcher

Sends ANAH notifications to Discord channels via webhooks. Supports:
- Real-time alerts (critical/warning/info)
- Heartbeat cycle summaries
- Health status embeds with color-coded severity

## Setup

1. Create a webhook in your Discord server (Channel Settings → Integrations → Webhooks)
2. Add to `~/.anah/.env`:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your/webhook/url
   ```

## Usage

```bash
# Send a test notification
python scripts/discord.py --test

# Send a custom notification
python scripts/discord.py --send --level info --title "Hello" --message "ANAH is online"

# Flush pending notifications from notifications.json
python scripts/discord.py --flush

# Watch mode: tail notifications.json and send new entries
python scripts/discord.py --watch
```
