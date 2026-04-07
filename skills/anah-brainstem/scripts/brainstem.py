#!/usr/bin/env python3
"""ANAH Brainstem — L1-L5 autonomic health checks.

Runs without LLM involvement. Pure signal monitoring.
Outputs JSON results to stdout and persists state to ~/.anah/state.json.
"""

import asyncio
import hashlib
import json
import os
import platform
import socket
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# State directory
# ---------------------------------------------------------------------------
ANAH_DIR = Path.home() / ".anah"
STATE_FILE = ANAH_DIR / "state.json"
CONFIG_FILE = ANAH_DIR / "config.json"
DB_FILE = ANAH_DIR / "anah.db"
BACKUP_DIR = ANAH_DIR / "backups"
SKILLS_DIR = ANAH_DIR / "skills"
LEARNING_LOG = ANAH_DIR / "learning_log.json"
TRAJECTORIES_DIR = ANAH_DIR / "trajectories"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def ensure_dirs():
    ANAH_DIR.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(exist_ok=True)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    default = {
        "thresholds": {
            "cpu_percent_max": 90,
            "ram_percent_max": 85,
            "disk_percent_max": 90,
            "dns_timeout_sec": 5,
            "api_ping_timeout_sec": 10,
        },
        "intervals": {
            "l1_heartbeat_sec": 30,
            "l2_check_sec": 300,
            "l3_check_sec": 900,
        },
        "integrations": [],
    }
    CONFIG_FILE.write_text(json.dumps(default, indent=2))
    return default


# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    level: int
    passed: bool
    duration_ms: float
    message: str
    details: dict | None = None


# ---------------------------------------------------------------------------
# L1 — Operational Survival
# ---------------------------------------------------------------------------
async def check_network(config: dict) -> CheckResult:
    start = time.monotonic()
    timeout = config.get("thresholds", {}).get("dns_timeout_sec", 5)
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, "dns.google", 443),
            timeout=timeout,
        )
        # TCP check
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        await loop.run_in_executor(None, sock.connect, ("8.8.8.8", 53))
        sock.close()
        ms = (time.monotonic() - start) * 1000
        return CheckResult("network_connectivity", 1, True, ms, "DNS and network reachable")
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("network_connectivity", 1, False, ms, f"Network check failed: {e}")


async def check_filesystem(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        test_file = ANAH_DIR / ".fs_check"
        test_file.write_text("anah_fs_check")
        content = test_file.read_text()
        test_file.unlink()
        passed = content == "anah_fs_check"
        ms = (time.monotonic() - start) * 1000
        return CheckResult("filesystem_access", 1, passed, ms, "Read/write OK" if passed else "R/W mismatch")
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("filesystem_access", 1, False, ms, f"Filesystem error: {e}")


async def check_compute(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent if platform.system() != "Windows" else psutil.disk_usage("C:\\").percent
        thresholds = config.get("thresholds", {})
        issues = []
        if cpu > thresholds.get("cpu_percent_max", 90):
            issues.append(f"CPU {cpu}%")
        if ram > thresholds.get("ram_percent_max", 85):
            issues.append(f"RAM {ram}%")
        if disk > thresholds.get("disk_percent_max", 90):
            issues.append(f"Disk {disk}%")
        ms = (time.monotonic() - start) * 1000
        passed = len(issues) == 0
        msg = f"CPU {cpu}%, RAM {ram}%, Disk {disk}%" if passed else f"Threshold exceeded: {', '.join(issues)}"
        return CheckResult("compute_resources", 1, passed, ms, msg, {"cpu": cpu, "ram": ram, "disk": disk})
    except ImportError:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("compute_resources", 1, False, ms, "psutil not installed")


async def check_wifi(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        import psutil
        interfaces = [name for name, addrs in psutil.net_if_addrs().items() if addrs]
        active = [name for name, stats in psutil.net_if_stats().items() if stats.isup and name in interfaces]
        ms = (time.monotonic() - start) * 1000
        if active:
            return CheckResult("wifi_interface", 1, True, ms, f"Active interfaces: {', '.join(active[:3])}")
        return CheckResult("wifi_interface", 1, False, ms, "No active network interfaces")
    except ImportError:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("wifi_interface", 1, False, ms, "psutil not installed")


async def run_l1(config: dict) -> list[CheckResult]:
    return list(await asyncio.gather(
        check_network(config),
        check_filesystem(config),
        check_compute(config),
        check_wifi(config),
    ))


# ---------------------------------------------------------------------------
# L2 — Persistent State Safety
# ---------------------------------------------------------------------------
async def check_config_integrity(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        if CONFIG_FILE.exists():
            content = CONFIG_FILE.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            # Check against stored checksum
            checksum_file = ANAH_DIR / ".config_checksum"
            if checksum_file.exists():
                stored = checksum_file.read_text().strip()
                if stored != checksum:
                    ms = (time.monotonic() - start) * 1000
                    checksum_file.write_text(checksum)
                    return CheckResult("config_integrity", 2, False, ms, "Config changed since last check", {"checksum": checksum})
            checksum_file.write_text(checksum)
            ms = (time.monotonic() - start) * 1000
            return CheckResult("config_integrity", 2, True, ms, "Config integrity OK", {"checksum": checksum})
        ms = (time.monotonic() - start) * 1000
        return CheckResult("config_integrity", 2, True, ms, "No config file (using defaults)")
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("config_integrity", 2, False, ms, f"Config check failed: {e}")


async def check_db_integrity(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        if not DB_FILE.exists():
            ms = (time.monotonic() - start) * 1000
            return CheckResult("db_integrity", 2, True, ms, "No database yet (will be created)")
        import sqlite3
        conn = sqlite3.connect(str(DB_FILE))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        ms = (time.monotonic() - start) * 1000
        passed = result[0] == "ok"
        return CheckResult("db_integrity", 2, passed, ms, "Database integrity OK" if passed else f"DB issue: {result[0]}")
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("db_integrity", 2, False, ms, f"DB check failed: {e}")


async def check_backup_recency(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        if not DB_FILE.exists():
            ms = (time.monotonic() - start) * 1000
            return CheckResult("backup_recency", 2, True, ms, "No database to back up")
        backups = sorted(BACKUP_DIR.glob("anah_backup_*.db"), reverse=True)
        max_age = 600  # 10 minutes
        if backups:
            age = time.time() - backups[0].stat().st_mtime
            if age < max_age:
                ms = (time.monotonic() - start) * 1000
                return CheckResult("backup_recency", 2, True, ms, f"Backup age: {int(age)}s")
        # Auto-repair: create backup
        import shutil
        backup_name = f"anah_backup_{int(time.time())}.db"
        shutil.copy2(str(DB_FILE), str(BACKUP_DIR / backup_name))
        ms = (time.monotonic() - start) * 1000
        return CheckResult("backup_recency", 2, True, ms, f"Backup created: {backup_name}", {"auto_repair": True})
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("backup_recency", 2, False, ms, f"Backup check failed: {e}")


async def run_l2(config: dict) -> list[CheckResult]:
    return list(await asyncio.gather(
        check_config_integrity(config),
        check_db_integrity(config),
        check_backup_recency(config),
    ))


# ---------------------------------------------------------------------------
# L3 — Task Ecosystem Health
# ---------------------------------------------------------------------------
async def check_anthropic_api(config: dict) -> CheckResult:
    start = time.monotonic()
    timeout = config.get("thresholds", {}).get("api_ping_timeout_sec", 10)
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            headers={"anthropic-version": "2023-06-01", "content-type": "application/json"},
            method="POST",
            data=b"{}",
        )
        try:
            urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            # 401/403/405 = API is reachable (just not authenticated)
            ms = (time.monotonic() - start) * 1000
            if e.code in (401, 403, 405):
                return CheckResult("anthropic_api", 3, True, ms, f"Anthropic API responded: {e.code}")
            return CheckResult("anthropic_api", 3, False, ms, f"Anthropic API error: {e.code}")
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("anthropic_api", 3, False, ms, f"API check failed: {e}")
    ms = (time.monotonic() - start) * 1000
    return CheckResult("anthropic_api", 3, True, ms, "Anthropic API reachable")


async def check_integration(name: str, url: str, method: str, expected_status: int, timeout: int) -> CheckResult:
    start = time.monotonic()
    try:
        import urllib.request
        req = urllib.request.Request(url, method=method)
        resp = urllib.request.urlopen(req, timeout=timeout)
        ms = (time.monotonic() - start) * 1000
        passed = resp.status == expected_status
        return CheckResult(f"integration_{name}", 3, passed, ms, f"{name}: {resp.status}")
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult(f"integration_{name}", 3, False, ms, f"{name} failed: {e}")


async def run_l3(config: dict) -> list[CheckResult]:
    checks = [check_anthropic_api(config)]
    timeout = config.get("thresholds", {}).get("api_ping_timeout_sec", 10)
    for integration in config.get("integrations", []):
        checks.append(check_integration(
            integration["name"], integration["url"],
            integration.get("method", "GET"),
            integration.get("expected_status", 200),
            timeout,
        ))
    return list(await asyncio.gather(*checks))


# ---------------------------------------------------------------------------
# L4 — Belonging / Integration
# ---------------------------------------------------------------------------
async def check_ollama_available(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        import urllib.request
        url = config.get("OLLAMA_URL", OLLAMA_URL)
        req = urllib.request.Request(f"{url}/api/tags", method="GET")
        timeout = config.get("thresholds", {}).get("api_ping_timeout_sec", 10)
        resp = urllib.request.urlopen(req, timeout=timeout)
        ms = (time.monotonic() - start) * 1000
        return CheckResult("ollama_available", 4, True, ms, f"Ollama API reachable ({resp.status})")
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("ollama_available", 4, False, ms, f"Ollama unreachable: {e}")


async def check_skills_ecosystem(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        if not SKILLS_DIR.exists():
            ms = (time.monotonic() - start) * 1000
            return CheckResult("skills_ecosystem", 4, False, ms, "Skills directory does not exist", {"skills_dir": str(SKILLS_DIR)})
        skill_dirs = [d for d in SKILLS_DIR.iterdir() if d.is_dir()]
        if len(skill_dirs) == 0:
            ms = (time.monotonic() - start) * 1000
            return CheckResult("skills_ecosystem", 4, False, ms, "No learned skills found", {"count": 0})
        # Validate SKILL.md frontmatter
        valid = 0
        invalid = []
        for sd in skill_dirs:
            skill_md = sd / "SKILL.md"
            if skill_md.exists():
                content = skill_md.read_text(encoding="utf-8", errors="replace")
                if content.strip().startswith("---"):
                    valid += 1
                else:
                    invalid.append(sd.name)
            else:
                invalid.append(sd.name)
        ms = (time.monotonic() - start) * 1000
        passed = valid >= 1
        msg = f"{valid} valid skill(s), {len(invalid)} invalid" if invalid else f"{valid} valid skill(s)"
        return CheckResult("skills_ecosystem", 4, passed, ms, msg, {"valid": valid, "invalid": invalid, "total": len(skill_dirs)})
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("skills_ecosystem", 4, False, ms, f"Skills check failed: {e}")


async def check_peer_connectivity(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        if not DB_FILE.exists():
            ms = (time.monotonic() - start) * 1000
            return CheckResult("peer_connectivity", 4, True, ms, "No database yet (no tasks to check)")
        import sqlite3
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - 3600  # last hour
        row = conn.execute(
            "SELECT COUNT(*) FROM task_queue WHERE status = 'completed' AND completed_at > ?",
            (cutoff,),
        ).fetchone()
        conn.close()
        count = row[0] if row else 0
        ms = (time.monotonic() - start) * 1000
        passed = count > 0
        msg = f"{count} task(s) completed in last hour" if passed else "No tasks completed in last hour"
        return CheckResult("peer_connectivity", 4, passed, ms, msg, {"completed_last_hour": count})
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("peer_connectivity", 4, False, ms, f"Peer connectivity check failed: {e}")


async def run_l4(config: dict) -> list[CheckResult]:
    return list(await asyncio.gather(
        check_ollama_available(config),
        check_skills_ecosystem(config),
        check_peer_connectivity(config),
    ))


# ---------------------------------------------------------------------------
# L5 — Self-Actualization
# ---------------------------------------------------------------------------
async def check_learning_rate(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        if not LEARNING_LOG.exists():
            ms = (time.monotonic() - start) * 1000
            return CheckResult("learning_rate", 5, False, ms, "No learning_log.json found", {"exists": False})
        data = json.loads(LEARNING_LOG.read_text())
        if not isinstance(data, list):
            data = data.get("entries", []) if isinstance(data, dict) else []
        cutoff = time.time() - 86400  # 24 hours
        recent = [e for e in data if e.get("timestamp", 0) > cutoff]
        ms = (time.monotonic() - start) * 1000
        passed = len(recent) > 0
        msg = f"{len(recent)} skill(s) learned in last 24h" if passed else "No skills learned in last 24h"
        return CheckResult("learning_rate", 5, passed, ms, msg, {"recent_count": len(recent), "total": len(data)})
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("learning_rate", 5, False, ms, f"Learning rate check failed: {e}")


async def check_goal_quality(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        if not DB_FILE.exists():
            ms = (time.monotonic() - start) * 1000
            return CheckResult("goal_quality", 5, True, ms, "No database yet (no goals to evaluate)")
        import sqlite3
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - 86400  # 24 hours
        total_row = conn.execute(
            "SELECT COUNT(*) FROM generated_goals WHERE timestamp > ?", (cutoff,)
        ).fetchone()
        enacted_row = conn.execute(
            "SELECT COUNT(*) FROM generated_goals WHERE timestamp > ? AND status = 'enacted'", (cutoff,)
        ).fetchone()
        conn.close()
        total = total_row[0] if total_row else 0
        enacted = enacted_row[0] if enacted_row else 0
        ms = (time.monotonic() - start) * 1000
        if total == 0:
            return CheckResult("goal_quality", 5, True, ms, "No goals in last 24h (OK)", {"total": 0, "enacted": 0})
        ratio = enacted / total
        passed = ratio > 0.5
        msg = f"{enacted}/{total} goals enacted ({ratio:.0%})"
        return CheckResult("goal_quality", 5, passed, ms, msg, {"total": total, "enacted": enacted, "ratio": round(ratio, 3)})
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("goal_quality", 5, False, ms, f"Goal quality check failed: {e}")


async def check_trajectory_growth(config: dict) -> CheckResult:
    start = time.monotonic()
    try:
        if not TRAJECTORIES_DIR.exists():
            ms = (time.monotonic() - start) * 1000
            return CheckResult("trajectory_growth", 5, False, ms, "Trajectories directory does not exist", {"trajectories_dir": str(TRAJECTORIES_DIR)})
        trajectories = list(TRAJECTORIES_DIR.iterdir())
        count = len(trajectories)
        ms = (time.monotonic() - start) * 1000
        passed = count > 0
        msg = f"{count} trajectory(ies) accumulated" if passed else "No trajectories yet"
        return CheckResult("trajectory_growth", 5, passed, ms, msg, {"count": count})
    except Exception as e:
        ms = (time.monotonic() - start) * 1000
        return CheckResult("trajectory_growth", 5, False, ms, f"Trajectory growth check failed: {e}")


async def run_l5(config: dict) -> list[CheckResult]:
    return list(await asyncio.gather(
        check_learning_rate(config),
        check_goal_quality(config),
        check_trajectory_growth(config),
    ))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"levels": {}, "last_update": 0, "gating": {"l1_healthy": False}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def run_checks(levels: list[int] | None = None) -> dict:
    ensure_dirs()
    config = load_config()
    state = load_state()

    if levels is None:
        levels = [1, 2, 3, 4, 5]

    all_results = []

    # Always run L1 first (gating)
    if 1 in levels:
        l1_results = await run_l1(config)
        all_results.extend(l1_results)
        l1_healthy = all(r.passed for r in l1_results)
        state["gating"] = {"l1_healthy": l1_healthy}
        state["levels"]["1"] = {
            "status": "healthy" if l1_healthy else "critical",
            "last_check": time.time(),
            "checks": [asdict(r) for r in l1_results],
        }

        # Gating: if L1 fails, suspend L2+
        if not l1_healthy:
            for lvl in [2, 3, 4, 5]:
                state["levels"].setdefault(str(lvl), {})["status"] = "suspended"
            state["last_update"] = time.time()
            save_state(state)
            return {"results": [asdict(r) for r in all_results], "gating": state["gating"], "state": state}

    if 2 in levels:
        l2_results = await run_l2(config)
        all_results.extend(l2_results)
        l2_healthy = all(r.passed for r in l2_results)
        state["levels"]["2"] = {
            "status": "healthy" if l2_healthy else "degraded",
            "last_check": time.time(),
            "checks": [asdict(r) for r in l2_results],
        }

    if 3 in levels:
        l3_results = await run_l3(config)
        all_results.extend(l3_results)
        l3_healthy = all(r.passed for r in l3_results)
        state["levels"]["3"] = {
            "status": "healthy" if l3_healthy else "degraded",
            "last_check": time.time(),
            "checks": [asdict(r) for r in l3_results],
        }

    if 4 in levels:
        l4_results = await run_l4(config)
        all_results.extend(l4_results)
        l4_healthy = all(r.passed for r in l4_results)
        state["levels"]["4"] = {
            "status": "healthy" if l4_healthy else "degraded",
            "last_check": time.time(),
            "checks": [asdict(r) for r in l4_results],
        }

    if 5 in levels:
        l5_results = await run_l5(config)
        all_results.extend(l5_results)
        l5_healthy = all(r.passed for r in l5_results)
        state["levels"]["5"] = {
            "status": "healthy" if l5_healthy else "aspirational",
            "last_check": time.time(),
            "checks": [asdict(r) for r in l5_results],
        }

    state["last_update"] = time.time()
    save_state(state)

    return {
        "results": [asdict(r) for r in all_results],
        "gating": state.get("gating", {}),
        "summary": {
            "total": len(all_results),
            "passed": sum(1 for r in all_results if r.passed),
            "failed": sum(1 for r in all_results if not r.passed),
            "health_score": round(sum(1 for r in all_results if r.passed) / len(all_results) * 100, 1) if all_results else 0,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANAH Brainstem — L1-L5 health checks")
    parser.add_argument("--level", "-l", type=int, choices=[1, 2, 3, 4, 5], help="Run a specific level only")
    parser.add_argument("--all", "-a", action="store_true", help="Run all levels")
    parser.add_argument("--compact", action="store_true", help="Compact JSON output")
    args = parser.parse_args()

    levels = [args.level] if args.level else [1, 2, 3, 4, 5]
    result = asyncio.run(run_checks(levels))

    indent = None if args.compact else 2
    print(json.dumps(result, indent=indent))
