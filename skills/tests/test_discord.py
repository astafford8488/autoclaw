"""Tests for anah-notify Discord dispatcher."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-notify" / "scripts"))
import discord as discord_notify


class TestEmbedBuilders:
    """Discord embed construction."""

    def test_notification_embed_critical(self):
        embed = discord_notify.build_notification_embed("critical", "Server Down", "L1 failed")
        assert embed["color"] == 0xED4245
        assert "Server Down" in embed["title"]
        assert embed["description"] == "L1 failed"

    def test_notification_embed_warning(self):
        embed = discord_notify.build_notification_embed("warning", "High Load")
        assert embed["color"] == 0xFEE75C

    def test_notification_embed_info(self):
        embed = discord_notify.build_notification_embed("info", "All Good", "Systems nominal")
        assert embed["color"] == 0x57F287

    def test_notification_embed_with_source(self):
        embed = discord_notify.build_notification_embed("info", "Test", source="scheduler")
        sources = [f for f in embed.get("fields", []) if f["name"] == "Source"]
        assert len(sources) == 1
        assert sources[0]["value"] == "scheduler"

    def test_notification_embed_with_timestamp(self):
        embed = discord_notify.build_notification_embed("info", "Test", timestamp=1700000000.0)
        assert "timestamp" in embed

    def test_notification_embed_truncates_long_message(self):
        long_msg = "x" * 5000
        embed = discord_notify.build_notification_embed("info", "Test", long_msg)
        assert len(embed["description"]) == 4096

    def test_heartbeat_embed_healthy(self):
        summary = {"gated": False, "health_score": 0.95, "goals_generated": 3,
                    "tasks_processed": 2, "tasks_succeeded": 2, "skills_extracted": 1,
                    "trajectories_exported": 5, "duration_ms": 800, "summary": "Health: 95%"}
        embed = discord_notify.build_heartbeat_embed(summary)
        assert embed["color"] == 0x57F287  # Green
        assert "Heartbeat" in embed["title"]
        field_names = [f["name"] for f in embed["fields"]]
        assert "Health" in field_names
        assert "Goals" in field_names
        assert "Tasks" in field_names
        assert "Skills" in field_names

    def test_heartbeat_embed_gated(self):
        summary = {"gated": True, "health_score": 0.3}
        embed = discord_notify.build_heartbeat_embed(summary)
        assert embed["color"] == 0xED4245  # Red
        assert "GATED" in embed["title"]

    def test_heartbeat_embed_degraded(self):
        summary = {"gated": False, "health_score": 0.6, "summary": "Degraded"}
        embed = discord_notify.build_heartbeat_embed(summary)
        assert embed["color"] == 0xFEE75C  # Yellow

    def test_heartbeat_embed_critical(self):
        summary = {"gated": False, "health_score": 0.2, "summary": "Critical"}
        embed = discord_notify.build_heartbeat_embed(summary)
        assert embed["color"] == 0xED4245  # Red

    def test_heartbeat_embed_normalizes_100_scale(self):
        """Brainstem returns health_score as 0-100, not 0-1."""
        summary = {"gated": False, "health_score": 95.0, "summary": "OK"}
        embed = discord_notify.build_heartbeat_embed(summary)
        assert embed["color"] == 0x57F287  # Green (95% is healthy)
        health_field = [f for f in embed["fields"] if f["name"] == "Health"][0]
        assert health_field["value"] == "95%"


class TestSendWebhook:
    """Webhook HTTP posting."""

    def test_send_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("discord.urllib.request.urlopen", return_value=mock_resp):
            assert discord_notify.send_webhook("https://example.com/webhook", {"content": "test"})

    def test_send_http_error(self):
        import urllib.error
        with patch("discord.urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("url", 429, "rate limited", {}, None)):
            assert not discord_notify.send_webhook("https://example.com/webhook", {})

    def test_send_network_error(self):
        with patch("discord.urllib.request.urlopen", side_effect=ConnectionError("timeout")):
            assert not discord_notify.send_webhook("https://example.com/webhook", {})


class TestSendNotification:
    """High-level send_notification."""

    def test_no_webhook_url(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(discord_notify, "load_env"):
            result = discord_notify.send_notification("info", "Test")
        assert result is False

    def test_sends_with_webhook(self):
        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://example.com/hook"}), \
             patch.object(discord_notify, "load_env"), \
             patch.object(discord_notify, "send_webhook", return_value=True) as mock_send:
            result = discord_notify.send_notification("info", "Test", "Hello")
        assert result is True
        mock_send.assert_called_once()
        payload = mock_send.call_args[0][1]
        assert "embeds" in payload
        assert len(payload["embeds"]) == 1


class TestSendHeartbeat:
    """High-level send_heartbeat."""

    def test_sends_heartbeat_embed(self):
        summary = {"gated": False, "health_score": 0.9, "summary": "OK"}
        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://example.com/hook"}), \
             patch.object(discord_notify, "load_env"), \
             patch.object(discord_notify, "send_webhook", return_value=True) as mock_send:
            result = discord_notify.send_heartbeat(summary)
        assert result is True
        payload = mock_send.call_args[0][1]
        assert "embeds" in payload


class TestFlush:
    """Flush pending notifications."""

    def test_flush_reads_and_sends(self, tmp_path):
        notif_file = tmp_path / "notifications.json"
        cursor_file = tmp_path / "discord_cursor.json"
        entries = [
            {"timestamp": 1000, "level": "info", "title": "Test 1", "message": "msg1"},
            {"timestamp": 1001, "level": "warning", "title": "Test 2", "message": "msg2"},
        ]
        notif_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://example.com/hook"}), \
             patch.object(discord_notify, "load_env"), \
             patch.object(discord_notify, "NOTIF_FILE", notif_file), \
             patch.object(discord_notify, "CURSOR_FILE", cursor_file), \
             patch.object(discord_notify, "send_webhook", return_value=True):
            count = discord_notify.flush_pending()
        assert count == 2

    def test_flush_skips_already_sent(self, tmp_path):
        notif_file = tmp_path / "notifications.json"
        cursor_file = tmp_path / "discord_cursor.json"
        entries = [
            {"timestamp": 1000, "level": "info", "title": "Old", "message": ""},
            {"timestamp": 1001, "level": "info", "title": "New", "message": ""},
        ]
        content = "\n".join(json.dumps(e) for e in entries) + "\n"
        notif_file.write_text(content)
        # Set cursor past first line
        first_line_end = len(json.dumps(entries[0]) + "\n")
        cursor_file.write_text(json.dumps({"offset": first_line_end}))

        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://example.com/hook"}), \
             patch.object(discord_notify, "load_env"), \
             patch.object(discord_notify, "NOTIF_FILE", notif_file), \
             patch.object(discord_notify, "CURSOR_FILE", cursor_file), \
             patch.object(discord_notify, "send_webhook", return_value=True):
            count = discord_notify.flush_pending()
        assert count == 1

    def test_flush_empty_file(self, tmp_path):
        notif_file = tmp_path / "notifications.json"
        cursor_file = tmp_path / "discord_cursor.json"
        notif_file.write_text("")

        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://example.com/hook"}), \
             patch.object(discord_notify, "load_env"), \
             patch.object(discord_notify, "NOTIF_FILE", notif_file), \
             patch.object(discord_notify, "CURSOR_FILE", cursor_file):
            count = discord_notify.flush_pending()
        assert count == 0

    def test_flush_no_file(self, tmp_path):
        with patch.object(discord_notify, "NOTIF_FILE", tmp_path / "nope.json"), \
             patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://example.com/hook"}), \
             patch.object(discord_notify, "load_env"):
            count = discord_notify.flush_pending()
        assert count == 0
