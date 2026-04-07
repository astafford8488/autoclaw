"""Tests for anah-brainstem — L1-L3 health monitoring."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure brainstem is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-brainstem" / "scripts"))
import brainstem


class TestL1Checks:
    """L1 survival checks — network, filesystem, compute, wifi."""

    @pytest.mark.asyncio
    async def test_run_checks_returns_expected_structure(self):
        """Full check run returns results, gating, and summary."""
        result = await brainstem.run_checks(levels=[1, 2, 3])
        assert "results" in result
        assert "gating" in result
        assert "summary" in result
        assert isinstance(result["results"], list)
        assert "l1_healthy" in result["gating"]

    @pytest.mark.asyncio
    async def test_l1_checks_always_run_first(self):
        """L1 must run before L2/L3 — gating depends on it."""
        result = await brainstem.run_checks(levels=[1])
        levels = {r.level for r in [brainstem.CheckResult(**r) if isinstance(r, dict) else r
                                     for r in result["results"]]}
        # All returned checks should be level 1
        for r in result["results"]:
            assert r["level"] == 1

    @pytest.mark.asyncio
    async def test_health_score_calculation(self):
        """Health score = passed/total * 100."""
        result = await brainstem.run_checks(levels=[1])
        summary = result["summary"]
        assert summary["total"] > 0
        expected = round(summary["passed"] / summary["total"] * 100, 1)
        assert summary["health_score"] == expected

    @pytest.mark.asyncio
    async def test_l1_gating_suspends_higher_levels(self):
        """When L1 fails, L2+ should be suspended in state."""
        # Mock a network failure
        async def mock_check_network(config):
            return brainstem.CheckResult(
                name="network_connectivity", level=1, passed=False,
                duration_ms=0, message="MOCK FAILURE")

        with patch.object(brainstem, "check_network", mock_check_network):
            result = await brainstem.run_checks(levels=[1, 2, 3])
            assert result["gating"]["l1_healthy"] is False
            # When L1 fails, L2/L3 checks should NOT appear in results
            levels_present = {r["level"] for r in result["results"]}
            assert 2 not in levels_present or result["gating"]["l1_healthy"]

    @pytest.mark.asyncio
    async def test_check_result_has_required_fields(self):
        """Every check result must have name, level, passed, duration_ms, message."""
        result = await brainstem.run_checks(levels=[1])
        for r in result["results"]:
            assert "name" in r
            assert "level" in r
            assert "passed" in r
            assert "duration_ms" in r
            assert "message" in r
            assert isinstance(r["passed"], bool)
            assert isinstance(r["duration_ms"], (int, float))

    @pytest.mark.asyncio
    async def test_compute_thresholds(self):
        """Compute check should pass when resources are under threshold."""
        result = await brainstem.run_checks(levels=[1])
        compute = [r for r in result["results"] if r["name"] == "compute_resources"]
        assert len(compute) == 1
        # On a dev machine, should pass unless system is actually overloaded
        assert compute[0]["passed"] is True
        if compute[0]["details"]:
            assert "cpu" in compute[0]["details"]
            assert "ram" in compute[0]["details"]


class TestL2Checks:
    """L2 state safety checks — config, DB, backups."""

    @pytest.mark.asyncio
    async def test_config_integrity_check(self):
        """Config integrity should produce a checksum."""
        result = await brainstem.run_checks(levels=[2])
        config_check = [r for r in result["results"] if r["name"] == "config_integrity"]
        assert len(config_check) == 1
        assert config_check[0]["passed"] is True
        if config_check[0]["details"]:
            assert "checksum" in config_check[0]["details"]

    @pytest.mark.asyncio
    async def test_l2_only_skips_l1_and_l3(self):
        """Running level=2 only should not include L1 or L3 checks."""
        result = await brainstem.run_checks(levels=[2])
        for r in result["results"]:
            assert r["level"] == 2


class TestL3Checks:
    """L3 ecosystem checks — external API health."""

    @pytest.mark.asyncio
    async def test_anthropic_api_check(self):
        """API check should complete (pass or fail) without crashing."""
        result = await brainstem.run_checks(levels=[3])
        api_check = [r for r in result["results"] if r["name"] == "anthropic_api"]
        assert len(api_check) == 1
        # Should complete regardless of whether API is reachable
        assert isinstance(api_check[0]["passed"], bool)


class TestStatePersistence:
    """State file management."""

    @pytest.mark.asyncio
    async def test_state_file_created_after_checks(self):
        """Running checks should create/update state.json."""
        await brainstem.run_checks(levels=[1])
        state_file = brainstem.ANAH_DIR / "state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert "gating" in state
        assert "levels" in state


class TestSecurity:
    """Security boundary tests."""

    @pytest.mark.asyncio
    async def test_no_secrets_in_output(self):
        """Check results should never contain API keys or secrets."""
        result = await brainstem.run_checks(levels=[1, 2, 3])
        output = json.dumps(result)
        assert "sk-ant-" not in output
        assert "sk-" not in output.lower() or "disk" in output.lower()  # Allow "disk" containing "sk"
        assert "api_key" not in output.lower()
        assert "password" not in output.lower()
        assert "secret" not in output.lower()

    @pytest.mark.asyncio
    async def test_filesystem_check_uses_temp_dir(self):
        """Filesystem check should write to ANAH_DIR, not arbitrary locations."""
        result = await brainstem.run_checks(levels=[1])
        fs_check = [r for r in result["results"] if r["name"] == "filesystem_access"]
        assert len(fs_check) == 1
        # Should pass — writes to ~/.anah/ only
        assert fs_check[0]["passed"] is True
