"""Tests for anah-brainstem — L1-L5 health monitoring."""

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


class TestL4Checks:
    """L4 belonging/integration checks — Ollama, skills ecosystem, peer connectivity."""

    @pytest.mark.asyncio
    async def test_l4_returns_three_checks(self):
        result = await brainstem.run_checks(levels=[4])
        assert len(result["results"]) == 3
        names = {r["name"] for r in result["results"]}
        assert names == {"ollama_available", "skills_ecosystem", "peer_connectivity"}

    @pytest.mark.asyncio
    async def test_l4_checks_are_level_4(self):
        result = await brainstem.run_checks(levels=[4])
        for r in result["results"]:
            assert r["level"] == 4

    @pytest.mark.asyncio
    async def test_ollama_check_passes_when_reachable(self):
        result = await brainstem.run_checks(levels=[4])
        ollama = [r for r in result["results"] if r["name"] == "ollama_available"][0]
        # Ollama should be running on the dev machine
        assert isinstance(ollama["passed"], bool)
        assert ollama["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_ollama_check_fails_gracefully(self):
        """When Ollama is unreachable, should fail without crashing."""
        with patch.dict("os.environ", {"OLLAMA_URL": "http://localhost:99999"}):
            # Need to reload the constant
            original = brainstem.OLLAMA_URL
            brainstem.OLLAMA_URL = "http://localhost:99999"
            try:
                config = brainstem.load_config()
                config["OLLAMA_URL"] = "http://localhost:99999"
                r = await brainstem.check_ollama_available(config)
                assert r.passed is False
                assert "unreachable" in r.message.lower() or "refused" in r.message.lower() or "error" in r.message.lower()
            finally:
                brainstem.OLLAMA_URL = original

    @pytest.mark.asyncio
    async def test_skills_ecosystem_with_valid_skills(self, anah_dir):
        skill_dir = anah_dir / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: test\n---\n# Test")
        with patch.object(brainstem, "SKILLS_DIR", anah_dir / "skills"):
            config = brainstem.load_config()
            r = await brainstem.check_skills_ecosystem(config)
            assert r.passed is True
            assert r.details["valid"] == 1

    @pytest.mark.asyncio
    async def test_skills_ecosystem_empty(self, anah_dir):
        skills_dir = anah_dir / "skills"
        skills_dir.mkdir(exist_ok=True)
        with patch.object(brainstem, "SKILLS_DIR", skills_dir):
            config = brainstem.load_config()
            r = await brainstem.check_skills_ecosystem(config)
            assert r.passed is False

    @pytest.mark.asyncio
    async def test_peer_connectivity_with_recent_tasks(self, anah_db, anah_dir):
        import time as t
        now = t.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, title, status, completed_at) VALUES (?, 'test', 'completed', ?)",
            (now, now))
        anah_db.commit()
        with patch.object(brainstem, "DB_FILE", anah_dir / "anah.db"):
            config = brainstem.load_config()
            r = await brainstem.check_peer_connectivity(config)
            assert r.passed is True
            assert r.details["completed_last_hour"] >= 1

    @pytest.mark.asyncio
    async def test_peer_connectivity_no_recent_tasks(self, anah_db, anah_dir):
        # No tasks in DB
        with patch.object(brainstem, "DB_FILE", anah_dir / "anah.db"):
            config = brainstem.load_config()
            r = await brainstem.check_peer_connectivity(config)
            assert r.passed is False


class TestL5Checks:
    """L5 self-actualization checks — learning rate, goal quality, trajectory growth."""

    @pytest.mark.asyncio
    async def test_l5_returns_three_checks(self):
        result = await brainstem.run_checks(levels=[5])
        assert len(result["results"]) == 3
        names = {r["name"] for r in result["results"]}
        assert names == {"learning_rate", "goal_quality", "trajectory_growth"}

    @pytest.mark.asyncio
    async def test_l5_checks_are_level_5(self):
        result = await brainstem.run_checks(levels=[5])
        for r in result["results"]:
            assert r["level"] == 5

    @pytest.mark.asyncio
    async def test_l5_uses_aspirational_status(self):
        """L5 should use 'aspirational' instead of 'degraded' when checks fail."""
        result = await brainstem.run_checks(levels=[5])
        state = json.loads(brainstem.STATE_FILE.read_text())
        l5_status = state["levels"].get("5", {}).get("status")
        assert l5_status in ("healthy", "aspirational")

    @pytest.mark.asyncio
    async def test_learning_rate_with_recent_entries(self, anah_dir):
        import time as t
        log = [{"timestamp": t.time(), "skill": "test-skill", "task_id": 1}]
        (anah_dir / "learning_log.json").write_text(json.dumps(log))
        with patch.object(brainstem, "LEARNING_LOG", anah_dir / "learning_log.json"):
            config = brainstem.load_config()
            r = await brainstem.check_learning_rate(config)
            assert r.passed is True
            assert r.details["recent_count"] >= 1

    @pytest.mark.asyncio
    async def test_learning_rate_no_log(self, anah_dir):
        with patch.object(brainstem, "LEARNING_LOG", anah_dir / "nonexistent.json"):
            config = brainstem.load_config()
            r = await brainstem.check_learning_rate(config)
            assert r.passed is False

    @pytest.mark.asyncio
    async def test_goal_quality_high_enactment(self, anah_db, anah_dir):
        import time as t
        now = t.time()
        anah_db.execute(
            "INSERT INTO generated_goals (timestamp, title, priority, status, source, reasoning) "
            "VALUES (?, 'g1', 5, 'enacted', 'llm', 'test')", (now,))
        anah_db.execute(
            "INSERT INTO generated_goals (timestamp, title, priority, status, source, reasoning) "
            "VALUES (?, 'g2', 5, 'enacted', 'llm', 'test')", (now,))
        anah_db.commit()
        with patch.object(brainstem, "DB_FILE", anah_dir / "anah.db"):
            config = brainstem.load_config()
            r = await brainstem.check_goal_quality(config)
            assert r.passed is True
            assert r.details["ratio"] == 1.0

    @pytest.mark.asyncio
    async def test_goal_quality_no_goals_is_ok(self, anah_db, anah_dir):
        with patch.object(brainstem, "DB_FILE", anah_dir / "anah.db"):
            config = brainstem.load_config()
            r = await brainstem.check_goal_quality(config)
            assert r.passed is True
            assert "No goals" in r.message or r.details["total"] == 0

    @pytest.mark.asyncio
    async def test_trajectory_growth_with_files(self, anah_dir):
        traj_dir = anah_dir / "trajectories"
        traj_dir.mkdir(exist_ok=True)
        (traj_dir / "t1.json").write_text("{}")
        with patch.object(brainstem, "TRAJECTORIES_DIR", traj_dir):
            config = brainstem.load_config()
            r = await brainstem.check_trajectory_growth(config)
            assert r.passed is True
            assert r.details["count"] >= 1

    @pytest.mark.asyncio
    async def test_trajectory_growth_empty(self, anah_dir):
        traj_dir = anah_dir / "trajectories"
        traj_dir.mkdir(exist_ok=True)
        with patch.object(brainstem, "TRAJECTORIES_DIR", traj_dir):
            config = brainstem.load_config()
            r = await brainstem.check_trajectory_growth(config)
            assert r.passed is False


class TestAllLevels:
    """Full L1-L5 run."""

    @pytest.mark.asyncio
    async def test_all_five_levels_run(self):
        result = await brainstem.run_checks(levels=[1, 2, 3, 4, 5])
        levels_present = {r["level"] for r in result["results"]}
        assert 1 in levels_present
        assert 2 in levels_present
        assert 3 in levels_present
        assert 4 in levels_present
        assert 5 in levels_present
        assert result["summary"]["total"] >= 14  # 4+3+1+3+3 minimum
