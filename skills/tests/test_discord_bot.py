"""Tests for anah-notify Discord bot — embed builders and helpers."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-notify" / "scripts"))

# Mock discord.py library before importing discord_bot
discord_mock = MagicMock()
discord_mock.Intents.default.return_value = MagicMock()
discord_mock.Client = MagicMock
discord_mock.ui = MagicMock()
discord_mock.app_commands = MagicMock()
discord_mock.ext = MagicMock()

with patch.dict(sys.modules, {
    "discord": discord_mock,
    "discord.ext": discord_mock.ext,
    "discord.ext.tasks": discord_mock.ext.tasks,
    "discord.ui": discord_mock.ui,
}):
    # Need to set HAS_DISCORD before import tries to use it
    import importlib
    # Force fresh import with mocked discord
    if "discord_bot" in sys.modules:
        del sys.modules["discord_bot"]
    import discord_bot


class TestPriorityInfo:
    def test_high_priority(self):
        label, expiry, color = discord_bot.get_priority_info(8)
        assert label == "High"
        assert "5min" in expiry
        assert color == 0xED4245

    def test_medium_priority(self):
        label, expiry, color = discord_bot.get_priority_info(5)
        assert label == "Medium"
        assert "15min" in expiry

    def test_low_priority(self):
        label, expiry, color = discord_bot.get_priority_info(2)
        assert label == "Low"
        assert "30min" in expiry

    def test_boundary_high(self):
        label, _, _ = discord_bot.get_priority_info(7)
        assert label == "High"

    def test_boundary_medium(self):
        label, _, _ = discord_bot.get_priority_info(4)
        assert label == "Medium"

    def test_boundary_low(self):
        label, _, _ = discord_bot.get_priority_info(0)
        assert label == "Low"


class TestStatusEmbed:
    def test_healthy_system(self):
        health = {"health_score": 95, "l1_healthy": True}
        queue_data = {"counts": {"queued": 2, "running": 1, "completed": 10}}
        goals = {"stats": {"total": 5, "pending_approval": 1}}
        embed = discord_bot.build_status_embed(health, queue_data, goals)
        assert embed["color"] == 0x57F287  # Green
        assert "95%" in embed["title"]
        assert len(embed["fields"]) >= 4

    def test_degraded_system(self):
        health = {"health_score": 60, "l1_healthy": True}
        embed = discord_bot.build_status_embed(health, {}, {})
        assert embed["color"] == 0xFEE75C  # Yellow

    def test_critical_system(self):
        health = {"health_score": 30, "l1_healthy": False}
        embed = discord_bot.build_status_embed(health, {}, {})
        assert embed["color"] == 0xED4245  # Red

    def test_gate_blocked(self):
        health = {"health_score": 50, "l1_healthy": False}
        embed = discord_bot.build_status_embed(health, {"counts": {}}, {"stats": {}})
        gate_field = [f for f in embed["fields"] if f["name"] == "L1 Gate"]
        assert len(gate_field) == 1
        assert "BLOCKED" in gate_field[0]["value"]


class TestGoalProposalEmbed:
    def test_high_priority_goal(self):
        goal = {"id": 1, "title": "Fix critical issue", "priority": 8,
                "source": "llm", "description": "System needs fix", "reasoning": "Health declining"}
        embed = discord_bot.build_goal_proposal_embed(goal)
        assert embed["color"] == 0xED4245  # Red for high
        assert "Goal Proposal #1" in embed["title"]
        assert "Fix critical issue" in embed["description"]
        assert any("High" in f["value"] for f in embed["fields"])

    def test_low_priority_goal(self):
        goal = {"id": 2, "title": "Cleanup logs", "priority": 2,
                "source": "pattern", "description": "", "reasoning": ""}
        embed = discord_bot.build_goal_proposal_embed(goal)
        assert any("Low" in f["value"] for f in embed["fields"])
        assert "30min" in embed["footer"]["text"]

    def test_truncates_long_description(self):
        goal = {"id": 3, "title": "Test", "priority": 5,
                "source": "llm", "description": "x" * 500, "reasoning": "y" * 500}
        embed = discord_bot.build_goal_proposal_embed(goal)
        desc_field = [f for f in embed["fields"] if f["name"] == "Description"][0]
        assert len(desc_field["value"]) <= 200

    def test_missing_description(self):
        goal = {"id": 4, "title": "Test", "priority": 5,
                "source": "llm", "description": None, "reasoning": None}
        embed = discord_bot.build_goal_proposal_embed(goal)
        desc_field = [f for f in embed["fields"] if f["name"] == "Description"][0]
        assert desc_field["value"] == "N/A"


class TestLoadEnv:
    def test_load_env(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_KEY=test_value\n# comment\nANOTHER=123\n")
        with patch.object(discord_bot, "ANAH_DIR", tmp_path):
            discord_bot.load_env()
        import os
        assert os.environ.get("TEST_KEY") == "test_value"
        assert os.environ.get("ANOTHER") == "123"
        # Cleanup
        os.environ.pop("TEST_KEY", None)
        os.environ.pop("ANOTHER", None)


class TestBotInstance:
    def test_get_bot_returns_none_initially(self):
        with patch.object(discord_bot, "_bot_instance", None):
            assert discord_bot.get_bot() is None
