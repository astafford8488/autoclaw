#!/usr/bin/env python3
"""ANAH Discord Notifier — Sends notifications to Discord via webhooks.

Supports:
- Direct sends (alerts, heartbeat summaries, status)
- Flushing pending notifications from ~/.anah/notifications.json
- Watch mode for real-time notification forwarding
- Embeds with color-coded severity levels

Requires DISCORD_WEBHOOK_URL in ~/.anah/.env or environment.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
NOTIF_FILE = ANAH_DIR / "notifications.json"
CURSOR_FILE = ANAH_DIR / "discord_cursor.json"

# Severity color map (Discord embed colors as decimal integers)
COLORS = {
    "critical": 0xED4245,   # Red
    "warning":  0xFEE75C,   # Yellow
    "info":     0x57F287,   # Green
    "success":  0x57F287,   # Green
    "error":    0xED4245,   # Red
}

LEVEL_EMOJI = {
    "critical": "\u274c",   # Red X
    "warning":  "\u26a0\ufe0f",    # Warning sign
    "info":     "\u2139\ufe0f",    # Info
    "success":  "\u2705",   # Check mark
    "error":    "\u274c",   # Red X
}


def load_env():
    """Load .env from ~/.anah/.env if present."""
    env_file = ANAH_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def get_webhook_url() -> str | None:
    """Get Discord webhook URL from environment."""
    return os.environ.get("DISCORD_WEBHOOK_URL")


def send_webhook(webhook_url: str, payload: dict, timeout: int = 10) -> bool:
    """POST a payload to a Discord webhook. Returns True on success."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "ANAH-Notify/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError as e:
        print(f"[discord] HTTP {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[discord] Error: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------
def build_notification_embed(level: str, title: str, message: str = "",
                              source: str = "", timestamp: float | None = None) -> dict:
    """Build a Discord embed for a notification entry."""
    color = COLORS.get(level, COLORS["info"])
    emoji = LEVEL_EMOJI.get(level, "")
    embed = {
        "title": f"{emoji} {title}",
        "color": color,
    }
    if message:
        embed["description"] = message[:4096]
    fields = []
    if source:
        fields.append({"name": "Source", "value": source, "inline": True})
    fields.append({"name": "Level", "value": level.upper(), "inline": True})
    if fields:
        embed["fields"] = fields
    if timestamp:
        from datetime import datetime, timezone
        embed["timestamp"] = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    embed["footer"] = {"text": "ANAH Autonomous Agent"}
    return embed


def build_heartbeat_embed(summary: dict) -> dict:
    """Build a Discord embed from a heartbeat cycle summary."""
    gated = summary.get("gated", False)
    health_raw = summary.get("health_score", 0)
    # Normalize: brainstem returns 0-100, but some callers pass 0.0-1.0
    health = health_raw / 100 if health_raw > 1 else health_raw

    if gated:
        color = COLORS["critical"]
        title = "\u274c ANAH Heartbeat — GATED"
        desc = "L1 failure detected. Higher brain functions skipped."
    elif health >= 0.8:
        color = COLORS["success"]
        title = "\u2705 ANAH Heartbeat"
        desc = summary.get("summary", "Cycle complete")
    elif health >= 0.5:
        color = COLORS["warning"]
        title = "\u26a0\ufe0f ANAH Heartbeat — Degraded"
        desc = summary.get("summary", "Cycle complete with issues")
    else:
        color = COLORS["critical"]
        title = "\u274c ANAH Heartbeat — Critical"
        desc = summary.get("summary", "Cycle complete with critical issues")

    fields = [
        {"name": "Health", "value": f"{health:.0%}", "inline": True},
    ]
    if summary.get("goals_generated"):
        fields.append({"name": "Goals", "value": str(summary["goals_generated"]), "inline": True})
    if summary.get("tasks_processed"):
        fields.append({
            "name": "Tasks",
            "value": f"{summary.get('tasks_succeeded', 0)}/{summary['tasks_processed']}",
            "inline": True,
        })
    if summary.get("skills_extracted"):
        fields.append({"name": "Skills", "value": f"+{summary['skills_extracted']}", "inline": True})
    if summary.get("trajectories_exported"):
        fields.append({"name": "Trajectories", "value": str(summary["trajectories_exported"]), "inline": True})
    if summary.get("duration_ms"):
        fields.append({"name": "Duration", "value": f"{summary['duration_ms']}ms", "inline": True})

    return {
        "title": title,
        "description": desc,
        "color": color,
        "fields": fields,
        "footer": {"text": "ANAH Autonomous Agent"},
    }


# ---------------------------------------------------------------------------
# High-level send functions
# ---------------------------------------------------------------------------
def send_notification(level: str, title: str, message: str = "",
                       source: str = "") -> bool:
    """Send a single notification to Discord."""
    load_env()
    url = get_webhook_url()
    if not url:
        print("[discord] No DISCORD_WEBHOOK_URL configured", file=sys.stderr)
        return False

    embed = build_notification_embed(level, title, message, source, time.time())
    return send_webhook(url, {"embeds": [embed]})


def send_heartbeat(summary: dict) -> bool:
    """Send a heartbeat summary to Discord."""
    load_env()
    url = get_webhook_url()
    if not url:
        return False

    embed = build_heartbeat_embed(summary)
    return send_webhook(url, {"embeds": [embed]})


# ---------------------------------------------------------------------------
# Flush & Watch — process notifications.json
# ---------------------------------------------------------------------------
def get_cursor() -> int:
    """Get the byte offset cursor for notifications.json."""
    if CURSOR_FILE.exists():
        try:
            data = json.loads(CURSOR_FILE.read_text())
            return data.get("offset", 0)
        except Exception:
            pass
    return 0


def save_cursor(offset: int):
    """Save byte offset cursor."""
    CURSOR_FILE.write_text(json.dumps({"offset": offset, "updated": time.time()}))


def flush_pending(max_batch: int = 10) -> int:
    """Send unsent notifications from notifications.json. Returns count sent."""
    load_env()
    url = get_webhook_url()
    if not url:
        print("[discord] No DISCORD_WEBHOOK_URL configured", file=sys.stderr)
        return 0

    if not NOTIF_FILE.exists():
        return 0

    cursor = get_cursor()
    sent = 0

    with open(str(NOTIF_FILE), "r", encoding="utf-8") as f:
        f.seek(cursor)
        remaining = f.read()

    lines = remaining.split("\n")
    embeds = []
    bytes_consumed = 0

    for line in lines:
        bytes_consumed += len(line.encode("utf-8")) + 1  # +1 for newline
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        embed = build_notification_embed(
            level=entry.get("level", "info"),
            title=entry.get("title", "Notification"),
            message=entry.get("message", ""),
            source=entry.get("source", ""),
            timestamp=entry.get("timestamp"),
        )
        embeds.append(embed)

        # Discord allows up to 10 embeds per message
        if len(embeds) >= max_batch:
            if send_webhook(url, {"embeds": embeds}):
                sent += len(embeds)
            embeds = []

    # Send remaining
    if embeds:
        if send_webhook(url, {"embeds": embeds}):
            sent += len(embeds)
    save_cursor(cursor + bytes_consumed)

    return sent


def watch(interval: float = 5.0):
    """Watch notifications.json and send new entries to Discord."""
    load_env()
    url = get_webhook_url()
    if not url:
        print("[discord] No DISCORD_WEBHOOK_URL configured", file=sys.stderr)
        return

    print(f"[discord] Watching {NOTIF_FILE} (Ctrl+C to stop)", file=sys.stderr)

    # Initial flush
    flushed = flush_pending()
    if flushed:
        print(f"[discord] Flushed {flushed} pending notifications", file=sys.stderr)

    try:
        while True:
            time.sleep(interval)
            sent = flush_pending()
            if sent:
                print(f"[discord] Sent {sent} notifications", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n[discord] Stopped", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ANAH Discord Notifier")
    parser.add_argument("--test", action="store_true", help="Send a test notification")
    parser.add_argument("--send", action="store_true", help="Send a custom notification")
    parser.add_argument("--level", default="info", choices=["info", "warning", "critical"])
    parser.add_argument("--title", default="ANAH Notification")
    parser.add_argument("--message", default="")
    parser.add_argument("--flush", action="store_true", help="Flush pending notifications")
    parser.add_argument("--watch", action="store_true", help="Watch and forward notifications")
    parser.add_argument("--interval", type=float, default=5.0, help="Watch poll interval (seconds)")
    args = parser.parse_args()

    if args.test:
        ok = send_notification("info", "ANAH Test", "Discord notifications are working!")
        print("Test sent!" if ok else "Failed to send test")
    elif args.send:
        ok = send_notification(args.level, args.title, args.message)
        print("Sent!" if ok else "Failed to send")
    elif args.flush:
        count = flush_pending()
        print(f"Flushed {count} notifications")
    elif args.watch:
        watch(args.interval)
    else:
        parser.print_help()
