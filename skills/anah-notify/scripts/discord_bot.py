#!/usr/bin/env python3
"""ANAH Discord Bot — Interactive Gateway bot for goal approval and system control.

Uses discord.py library (pip install discord.py) for Gateway websocket connection.
Provides slash commands and button interactions for goal management.

Commands:
  /anah status   — Show system health overview
  /anah cycle    — Trigger a heartbeat cycle
  /anah approve  — Approve a pending goal by ID
  /anah dismiss  — Dismiss a goal by ID
  /anah goals    — List pending goals

Requires in ~/.anah/.env:
  DISCORD_BOT_TOKEN=...
  DISCORD_APP_ID=...
  DISCORD_APPROVAL_CHANNEL_ID=...  (optional, for goal proposal embeds)
"""

import json
import os
import sqlite3
import sys
import time
import asyncio
from pathlib import Path

# Ensure sibling skill scripts are importable
SKILLS_DIR = Path(__file__).resolve().parent.parent.parent
for d in SKILLS_DIR.glob("anah-*/scripts"):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

ANAH_DIR = Path.home() / ".anah"
DB_FILE = ANAH_DIR / "anah.db"

# Try importing discord.py library
try:
    import discord
    from discord import app_commands
    from discord.ext import tasks
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False
    print("[discord_bot] discord.py not installed. Run: pip install discord.py", file=sys.stderr)


def load_env():
    """Load environment from ~/.anah/.env."""
    env_file = ANAH_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    return db


# ---------------------------------------------------------------------------
# Embed builders (reuse colors from webhook module)
# ---------------------------------------------------------------------------
COLORS = {
    "critical": 0xED4245,
    "warning": 0xFEE75C,
    "info": 0x57F287,
    "healthy": 0x57F287,
    "degraded": 0xFEE75C,
    "gated": 0xED4245,
}

PRIORITY_LABELS = {
    range(0, 4): ("Low", "auto-dismiss 30min", 0x30363d),
    range(4, 7): ("Medium", "auto-enact 15min", 0xFEE75C),
    range(7, 10): ("High", "auto-enact 5min", 0xED4245),
}


def get_priority_info(priority: int) -> tuple[str, str, int]:
    """Get (label, expiry_text, color) for a priority level."""
    for prange, info in PRIORITY_LABELS.items():
        if priority in prange:
            return info
    return ("Unknown", "", 0x30363d)


def build_status_embed(health: dict, queue: dict, goals: dict) -> dict:
    """Build a status overview embed dict (for discord.py Embed)."""
    score = health.get("health_score", 0)
    color = COLORS["healthy"] if score >= 80 else COLORS["degraded"] if score >= 50 else COLORS["critical"]
    return {
        "title": f"ANAH Status — {score:.0f}%",
        "color": color,
        "fields": [
            {"name": "Health", "value": f"{score:.0f}%", "inline": True},
            {"name": "L1 Gate", "value": "Open" if health.get("l1_healthy") else "BLOCKED", "inline": True},
            {"name": "Tasks", "value": f"Q:{queue.get('counts', {}).get('queued', 0)} R:{queue.get('counts', {}).get('running', 0)} D:{queue.get('counts', {}).get('completed', 0)}", "inline": True},
            {"name": "Goals", "value": f"Total:{goals.get('stats', {}).get('total', 0)} Pending:{goals.get('stats', {}).get('pending_approval', 0)}", "inline": True},
        ],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_goal_proposal_embed(goal: dict) -> dict:
    """Build an embed for a goal awaiting approval."""
    label, expiry_text, color = get_priority_info(goal.get("priority", 0))
    return {
        "title": f"Goal Proposal #{goal['id']}",
        "description": goal.get("title", ""),
        "color": color,
        "fields": [
            {"name": "Priority", "value": f"{goal.get('priority', 0)} ({label})", "inline": True},
            {"name": "Source", "value": goal.get("source", "unknown"), "inline": True},
            {"name": "Expiry", "value": expiry_text, "inline": True},
            {"name": "Description", "value": (goal.get("description", "") or "N/A")[:200], "inline": False},
            {"name": "Reasoning", "value": (goal.get("reasoning", "") or "N/A")[:200], "inline": False},
        ],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "footer": {"text": f"ID: {goal['id']} | {expiry_text}"},
    }


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------
if HAS_DISCORD:

    class ANAHBot(discord.Client):
        def __init__(self):
            intents = discord.Intents.default()
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)
            self.approval_channel_id = None

        async def setup_hook(self):
            """Register slash commands, restore persistent views, and start background tasks."""
            self.approval_channel_id = os.environ.get("DISCORD_APPROVAL_CHANNEL_ID")
            if self.approval_channel_id:
                self.approval_channel_id = int(self.approval_channel_id)

            # Re-register persistent views for any pending goals with discord messages
            # This is what makes buttons survive bot restarts
            try:
                db = get_db()
                pending = db.execute(
                    "SELECT id FROM generated_goals "
                    "WHERE status = 'pending_approval' AND discord_message_id IS NOT NULL"
                ).fetchall()
                db.close()
                for row in pending:
                    self.add_view(GoalApprovalView(row["id"]))
                if pending:
                    print(f"[discord_bot] Restored {len(pending)} persistent approval views", file=sys.stderr)
            except Exception as e:
                print(f"[discord_bot] Could not restore views: {e}", file=sys.stderr)

            # Register commands
            _register_commands(self.tree)
            await self.tree.sync()
            print("[discord_bot] Slash commands synced", file=sys.stderr)

            # Start expiry checker
            self.check_expiry_loop.start()

        async def on_ready(self):
            print(f"[discord_bot] Logged in as {self.user} (ID: {self.user.id})", file=sys.stderr)

        @tasks.loop(seconds=30)
        async def check_expiry_loop(self):
            """Periodically check for expired goal approvals and post un-posted proposals."""
            try:
                import cortex
                db = get_db()

                # Post any pending_approval goals that haven't been posted to Discord yet
                if self.approval_channel_id:
                    unposted = db.execute(
                        "SELECT * FROM generated_goals "
                        "WHERE status = 'pending_approval' AND discord_message_id IS NULL "
                        "ORDER BY priority DESC LIMIT 5"
                    ).fetchall()
                    for row in unposted:
                        goal = dict(row)
                        try:
                            await self.post_goal_proposal(goal)
                            print(f"[discord_bot] Posted goal #{goal['id']} to approval channel",
                                  file=sys.stderr)
                        except Exception as e:
                            print(f"[discord_bot] Failed to post goal #{goal['id']}: {e}",
                                  file=sys.stderr)

                # Check expired approvals
                result = cortex.check_expired_approvals(db)
                db.close()
                if result["enacted"] or result["dismissed"]:
                    print(f"[discord_bot] Expiry: {result['enacted']} enacted, {result['dismissed']} dismissed",
                          file=sys.stderr)
                    # Update approval channel messages
                    if self.approval_channel_id:
                        await self._update_expired_messages(result)
            except Exception as e:
                print(f"[discord_bot] Expiry check error: {e}", file=sys.stderr)

        @check_expiry_loop.before_loop
        async def before_check_expiry(self):
            await self.wait_until_ready()

        async def _update_expired_messages(self, result):
            """Edit expired goal messages in approval channel."""
            # Best-effort: find and edit messages
            pass  # Will be enhanced when we track discord_message_id

        async def post_goal_proposal(self, goal: dict):
            """Post a goal proposal to the approval channel with buttons."""
            if not self.approval_channel_id:
                return None
            channel = self.get_channel(self.approval_channel_id)
            if not channel:
                return None

            embed_data = build_goal_proposal_embed(goal)
            embed = discord.Embed(
                title=embed_data["title"],
                description=embed_data["description"],
                color=embed_data["color"],
            )
            for field in embed_data["fields"]:
                embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", True))
            embed.set_footer(text=embed_data.get("footer", {}).get("text", ""))

            view = GoalApprovalView(goal["id"])
            msg = await channel.send(embed=embed, view=view)

            # Store discord_message_id for later editing
            try:
                db = get_db()
                db.execute(
                    "UPDATE generated_goals SET discord_message_id = ? WHERE id = ?",
                    (str(msg.id), goal["id"]),
                )
                db.commit()
                db.close()
            except Exception:
                pass

            return msg.id


    class GoalApprovalView(discord.ui.View):
        """Buttons for approving/dismissing a goal.

        Uses dynamic custom_id with goal_id prefix so views survive bot restarts.
        Discord.py persistent views require: timeout=None + custom_id on every item.
        """

        def __init__(self, goal_id: int):
            super().__init__(timeout=None)  # Persistent view
            self.goal_id = goal_id

            # Build buttons with dynamic custom_id (required for persistence)
            approve_btn = discord.ui.Button(
                label="Approve", style=discord.ButtonStyle.green,
                custom_id=f"anah_approve_{goal_id}",
            )
            approve_btn.callback = self.approve_callback
            self.add_item(approve_btn)

            dismiss_btn = discord.ui.Button(
                label="Dismiss", style=discord.ButtonStyle.red,
                custom_id=f"anah_dismiss_{goal_id}",
            )
            dismiss_btn.callback = self.dismiss_callback
            self.add_item(dismiss_btn)

        async def approve_callback(self, interaction: discord.Interaction):
            try:
                import cortex
                db = get_db()
                result = cortex.approve_goal(db, self.goal_id)
                db.close()
                if "error" in result:
                    await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
                else:
                    embed = interaction.message.embeds[0] if interaction.message.embeds else None
                    if embed:
                        embed.color = 0x57F287
                        embed.title = f"Goal #{self.goal_id} — APPROVED"
                        embed.set_footer(text=f"Approved by {interaction.user.name}")
                    self.clear_items()
                    await interaction.response.edit_message(embed=embed, view=self)
            except Exception as e:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)

        async def dismiss_callback(self, interaction: discord.Interaction):
            try:
                import cortex
                db = get_db()
                cortex.dismiss_goal(db, self.goal_id)
                db.close()
                # Edit the original message
                embed = interaction.message.embeds[0] if interaction.message.embeds else None
                if embed:
                    embed.color = 0x30363d
                    embed.title = f"Goal #{self.goal_id} — DISMISSED"
                    embed.set_footer(text=f"Dismissed by {interaction.user.name}")
                self.clear_items()
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception as e:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)


    def _register_commands(tree: app_commands.CommandTree):
        """Register all /anah slash commands."""

        group = app_commands.Group(name="anah", description="ANAH Brain Control")

        @group.command(name="status", description="Show system health overview")
        async def cmd_status(interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                sys.path.insert(0, str(SKILLS_DIR / "anah-dashboard" / "scripts"))
                import dashboard
                health = dashboard.api_health()
                queue_data = dashboard.api_queue()
                goals = dashboard.api_goals()
                embed_data = build_status_embed(health, queue_data, goals)
                embed = discord.Embed(
                    title=embed_data["title"],
                    color=embed_data["color"],
                )
                for f in embed_data["fields"]:
                    embed.add_field(name=f["name"], value=f["value"], inline=f.get("inline", True))
                await interaction.followup.send(embed=embed)
            except Exception as e:
                await interaction.followup.send(f"Error: {e}")

        @group.command(name="cycle", description="Trigger a heartbeat cycle")
        async def cmd_cycle(interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                sys.path.insert(0, str(SKILLS_DIR / "anah-orchestrator" / "scripts"))
                import orchestrator
                orchestrator.load_env()
                result = orchestrator.full_cycle(generate=True, execute=True, learn=True)
                duration = result.get("duration_ms", 0)
                score = result.get("brainstem", {}).get("health_score", 0)
                goals_count = result.get("cortex", {}).get("count", 0)
                await interaction.followup.send(
                    f"Cycle complete ({duration:.0f}ms) — Health: {score:.0f}%, Goals: {goals_count}"
                )
            except Exception as e:
                await interaction.followup.send(f"Error: {e}")

        @group.command(name="approve", description="Approve a pending goal")
        @app_commands.describe(goal_id="The goal ID to approve")
        async def cmd_approve(interaction: discord.Interaction, goal_id: int):
            try:
                import cortex
                db = get_db()
                result = cortex.approve_goal(db, goal_id)
                db.close()
                if "error" in result:
                    await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
                else:
                    await interaction.response.send_message(
                        f"Goal #{goal_id} approved — Task #{result['task_id']} created"
                    )
            except Exception as e:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)

        @group.command(name="dismiss", description="Dismiss a goal")
        @app_commands.describe(goal_id="The goal ID to dismiss")
        async def cmd_dismiss(interaction: discord.Interaction, goal_id: int):
            try:
                import cortex
                db = get_db()
                cortex.dismiss_goal(db, goal_id)
                db.close()
                await interaction.response.send_message(f"Goal #{goal_id} dismissed")
            except Exception as e:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)

        @group.command(name="goals", description="List pending goals")
        async def cmd_goals(interaction: discord.Interaction):
            db = get_db()
            rows = db.execute(
                "SELECT id, title, priority, status, expires_at FROM generated_goals "
                "WHERE status IN ('pending_approval', 'proposed') ORDER BY priority DESC LIMIT 10"
            ).fetchall()
            db.close()
            if not rows:
                await interaction.response.send_message("No pending goals")
                return
            lines = []
            for r in rows:
                r = dict(r)
                label, _, _ = get_priority_info(r["priority"])
                exp = ""
                if r.get("expires_at"):
                    remaining = max(0, int(r["expires_at"] - time.time()))
                    exp = f" ({remaining}s left)" if remaining > 0 else " (expired)"
                lines.append(f"#{r['id']} [P{r['priority']}/{label}] {r['title']}{exp}")
            await interaction.response.send_message("**Pending Goals:**\n" + "\n".join(lines))

        tree.add_command(group)


# ---------------------------------------------------------------------------
# Functions callable from other modules (e.g. cortex after generating goals)
# ---------------------------------------------------------------------------
_bot_instance = None


def get_bot() -> "ANAHBot | None":
    """Get the running bot instance (if any)."""
    return _bot_instance


async def post_goal_to_approval_channel(goal: dict) -> int | None:
    """Post a goal proposal to Discord approval channel. Returns message ID or None."""
    bot = get_bot()
    if bot and bot.is_ready():
        return await bot.post_goal_proposal(goal)
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not HAS_DISCORD:
        print("ERROR: discord.py not installed. Run: pip install discord.py", file=sys.stderr)
        sys.exit(1)

    load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN not set in ~/.anah/.env or environment", file=sys.stderr)
        sys.exit(1)

    bot = ANAHBot()
    _bot_instance = bot

    print("[discord_bot] Starting ANAH Discord Bot...", file=sys.stderr)
    bot.run(token, log_handler=None)
