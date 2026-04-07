"""Tests for anah-hippocampus — autonomous skill creation and learning."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-hippocampus" / "scripts"))
import hippocampus


class TestComplexityAssessment:
    """Task complexity scoring for skill extraction decisions."""

    def test_simple_task_low_complexity(self):
        task = hippocampus.TaskEvidence(
            task_id=1, title="echo: hello", description="",
            source="manual", status="completed", duration_ms=50,
            result={"message": "hello"}, created_at=time.time())
        score = hippocampus.assess_complexity(task)
        assert score < hippocampus.MIN_STEPS_FOR_SKILL

    def test_complex_task_high_complexity(self):
        task = hippocampus.TaskEvidence(
            task_id=1, title="health_report: full system diagnostic",
            description="Run all checks", source="l5_generated",
            status="completed", duration_ms=5000,
            result={
                "diagnostic": {
                    "l1": [{"name": "net", "passed": True}, {"name": "fs", "passed": True}],
                    "l2": [{"name": "config", "passed": True}],
                    "l3": [{"name": "api", "passed": True}],
                },
                "summary": {"health_score": 100},
            },
            created_at=time.time())
        score = hippocampus.assess_complexity(task)
        assert score >= hippocampus.MIN_STEPS_FOR_SKILL

    def test_long_duration_adds_complexity(self):
        short = hippocampus.TaskEvidence(
            task_id=1, title="test", description="", source="manual",
            status="completed", duration_ms=100, result={"a": 1}, created_at=time.time())
        long = hippocampus.TaskEvidence(
            task_id=2, title="test", description="", source="manual",
            status="completed", duration_ms=5000, result={"a": 1}, created_at=time.time())
        assert hippocampus.assess_complexity(long) > hippocampus.assess_complexity(short)


class TestSkillExtraction:
    """Deciding whether to extract a skill from task evidence."""

    def test_incomplete_task_rejected(self):
        task = hippocampus.TaskEvidence(
            task_id=1, title="test", description="", source="manual",
            status="failed", duration_ms=None, result=None, created_at=time.time())
        should, reason = hippocampus.should_extract_skill(task)
        assert should is False
        assert "not completed" in reason

    def test_simple_task_rejected(self):
        task = hippocampus.TaskEvidence(
            task_id=1, title="echo: hi", description="", source="manual",
            status="completed", duration_ms=10, result={"msg": "hi"},
            created_at=time.time())
        should, reason = hippocampus.should_extract_skill(task)
        assert should is False
        assert "too simple" in reason


class TestSkillGeneration:
    """Generating skill candidates from task evidence."""

    def test_generate_skill_candidate(self):
        task = hippocampus.TaskEvidence(
            task_id=42, title="health_report: full diagnostic",
            description="Complete system health check",
            source="l5_generated", status="completed", duration_ms=3000,
            result={"summary": {"score": 100}, "checks": {"a": 1, "b": 2, "c": 3}},
            created_at=time.time())
        candidate = hippocampus.generate_skill_candidate(task)
        assert candidate is not None
        assert candidate.name.startswith("learned-")
        assert candidate.evidence_task_id == 42
        assert 0 <= candidate.confidence <= 1.0

    def test_candidate_category_detection(self):
        """Task titles should map to correct categories."""
        diagnostic_task = hippocampus.TaskEvidence(
            task_id=1, title="self_diagnostic: investigate slowdown",
            description="", source="manual", status="completed",
            duration_ms=1000, result={}, created_at=time.time())
        candidate = hippocampus.generate_skill_candidate(diagnostic_task)
        assert candidate.category == "diagnostic"

    def test_candidate_name_sanitization(self):
        """Skill names should be URL-safe slugs."""
        task = hippocampus.TaskEvidence(
            task_id=1, title="health_report: System Health & Performance!!!",
            description="", source="manual", status="completed",
            duration_ms=1000, result={}, created_at=time.time())
        candidate = hippocampus.generate_skill_candidate(task)
        assert " " not in candidate.name
        assert "&" not in candidate.name
        assert "!" not in candidate.name


class TestSkillWriting:
    """Writing skills to disk."""

    def test_write_skill_creates_directory(self, anah_dir):
        with patch.object(hippocampus, "SKILLS_DIR", anah_dir / "skills"):
            candidate = hippocampus.SkillCandidate(
                name="learned-test-skill",
                description="Test skill",
                instructions="## Test\nDo the thing.",
                category="diagnostic",
                confidence=0.8,
                evidence_task_id=1)
            hippocampus.write_skill(candidate)

            skill_dir = anah_dir / "skills" / "learned-test-skill"
            assert skill_dir.exists()
            assert (skill_dir / "SKILL.md").exists()

    def test_written_skill_has_valid_frontmatter(self, anah_dir):
        with patch.object(hippocampus, "SKILLS_DIR", anah_dir / "skills"):
            candidate = hippocampus.SkillCandidate(
                name="learned-valid-fm",
                description="Valid frontmatter skill",
                instructions="## Procedure\nSteps here.",
                category="monitoring",
                confidence=0.6,
                evidence_task_id=5)
            hippocampus.write_skill(candidate)

            content = (anah_dir / "skills" / "learned-valid-fm" / "SKILL.md").read_text()
            assert content.startswith("---\n")
            assert "name: learned-valid-fm" in content
            assert "description:" in content


class TestLearningLog:
    """Learning log persistence."""

    def test_log_learning_creates_file(self, anah_dir):
        with patch.object(hippocampus, "LEARNING_LOG", anah_dir / "learning_log.json"):
            candidate = hippocampus.SkillCandidate(
                "test", "desc", "inst", "diagnostic", 0.5, 1)
            hippocampus.log_learning(candidate, "created")
            log_file = anah_dir / "learning_log.json"
            assert log_file.exists()
            entries = json.loads(log_file.read_text())
            assert len(entries) == 1
            assert entries[0]["action"] == "created"

    def test_log_learning_caps_at_100(self, anah_dir):
        with patch.object(hippocampus, "LEARNING_LOG", anah_dir / "learning_log.json"):
            # Write 105 entries
            candidate = hippocampus.SkillCandidate(
                "test", "desc", "inst", "diagnostic", 0.5, 1)
            for i in range(105):
                hippocampus.log_learning(candidate, f"action_{i}")
            entries = json.loads((anah_dir / "learning_log.json").read_text())
            assert len(entries) == 100


class TestSecurity:
    """Security tests."""

    def test_skill_name_no_path_traversal(self):
        """Skill names with path traversal should be sanitized."""
        task = hippocampus.TaskEvidence(
            task_id=1, title="../../etc/passwd",
            description="", source="manual", status="completed",
            duration_ms=1000, result={}, created_at=time.time())
        candidate = hippocampus.generate_skill_candidate(task)
        assert ".." not in candidate.name
        assert "/" not in candidate.name
        assert "\\" not in candidate.name
