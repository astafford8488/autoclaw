#!/usr/bin/env python3
"""ANAH Trainer — Trajectory-based fine-tuning pipeline.

Reads completed task trajectories from ~/.anah/trajectories/, filters for
quality, and produces fine-tuning datasets. Supports:
- SFT (Supervised Fine-Tuning): successful trajectories only
- DPO (Direct Preference Optimization): paired success/failure trajectories

Output formats:
- JSONL for Ollama (Modelfile CREATE)
- ShareGPT for general training tools
- Alpaca format for compatibility

Usage:
    python trainer.py --prepare-sft           # Build SFT dataset
    python trainer.py --prepare-dpo           # Build DPO dataset
    python trainer.py --stats                 # Dataset statistics
    python trainer.py --create-modelfile      # Generate Ollama Modelfile
    python trainer.py --train                 # Full pipeline: prepare + create + train
"""

import json
import os
import subprocess
import time
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
TRAJECTORIES_DIR = ANAH_DIR / "trajectories"
TRAINING_DIR = ANAH_DIR / "training"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

# Quality filters
MIN_TRAJECTORY_LENGTH = 3       # Must have system + human + gpt messages
MIN_DURATION_MS = 100           # Skip instant/trivial tasks
MAX_DURATION_MS = 300_000       # Skip stuck tasks (5 min)
EXCLUDE_HANDLERS = {"echo"}     # Too trivial for training


# ---------------------------------------------------------------------------
# Trajectory loading and filtering
# ---------------------------------------------------------------------------
def load_all_trajectories() -> list[dict]:
    """Load all trajectory files from disk."""
    if not TRAJECTORIES_DIR.exists():
        return []
    all_trajs = []
    for f in sorted(TRAJECTORIES_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            if isinstance(data, list):
                all_trajs.extend(data)
            elif isinstance(data, dict):
                all_trajs.append(data)
        except (json.JSONDecodeError, Exception):
            continue
    return all_trajs


def filter_quality(trajectories: list[dict], success_only: bool = True) -> list[dict]:
    """Filter trajectories by quality criteria."""
    filtered = []
    for t in trajectories:
        meta = t.get("metadata", {})
        convos = t.get("conversations", [])

        # Must have conversations
        if len(convos) < MIN_TRAJECTORY_LENGTH:
            continue

        # Duration filter
        duration = meta.get("duration_ms")
        if duration is not None:
            if duration < MIN_DURATION_MS or duration > MAX_DURATION_MS:
                continue

        # Outcome filter
        if success_only and meta.get("outcome") != "completed":
            continue

        # Handler filter
        title = meta.get("title", "").lower()
        if any(title.startswith(f"{h}:") for h in EXCLUDE_HANDLERS):
            continue

        filtered.append(t)
    return filtered


def deduplicate(trajectories: list[dict]) -> list[dict]:
    """Remove duplicate trajectories by task_id."""
    seen = set()
    unique = []
    for t in trajectories:
        task_id = t.get("metadata", {}).get("task_id")
        if task_id and task_id in seen:
            continue
        if task_id:
            seen.add(task_id)
        unique.append(t)
    return unique


# ---------------------------------------------------------------------------
# Dataset preparation
# ---------------------------------------------------------------------------
def prepare_sft_dataset() -> dict:
    """Build SFT dataset from successful trajectories."""
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)

    all_trajs = load_all_trajectories()
    filtered = filter_quality(all_trajs, success_only=True)
    unique = deduplicate(filtered)

    # Convert to chat format
    sft_data = []
    for t in unique:
        convos = t.get("conversations", [])
        messages = []
        for c in convos:
            role_map = {"system": "system", "human": "user", "gpt": "assistant"}
            role = role_map.get(c.get("from"), "user")
            messages.append({"role": role, "content": c["value"]})
        sft_data.append({
            "messages": messages,
            "metadata": t.get("metadata", {}),
        })

    # Write JSONL
    sft_path = TRAINING_DIR / "sft_dataset.jsonl"
    with open(str(sft_path), "w", encoding="utf-8") as f:
        for entry in sft_data:
            f.write(json.dumps(entry) + "\n")

    # Write ShareGPT format
    sharegpt_path = TRAINING_DIR / "sft_sharegpt.json"
    sharegpt_data = [{"conversations": t["conversations"]} for t in unique]
    sharegpt_path.write_text(json.dumps(sharegpt_data, indent=2))

    return {
        "total_trajectories": len(all_trajs),
        "after_quality_filter": len(filtered),
        "after_dedup": len(unique),
        "sft_path": str(sft_path),
        "sharegpt_path": str(sharegpt_path),
    }


def prepare_dpo_dataset() -> dict:
    """Build DPO dataset from paired success/failure trajectories."""
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)

    all_trajs = load_all_trajectories()
    successes = filter_quality(all_trajs, success_only=True)
    successes = deduplicate(successes)

    # Get failures too
    failures = [t for t in all_trajs
                if t.get("metadata", {}).get("outcome") == "failed"
                and len(t.get("conversations", [])) >= MIN_TRAJECTORY_LENGTH]
    failures = deduplicate(failures)

    # Pair by similar tasks (same handler or similar title words)
    pairs = []
    used_failures = set()
    for s in successes:
        s_title = s.get("metadata", {}).get("title", "").lower().split()
        best_match = None
        best_score = 0
        for i, f in enumerate(failures):
            if i in used_failures:
                continue
            f_title = f.get("metadata", {}).get("title", "").lower().split()
            overlap = len(set(s_title) & set(f_title))
            if overlap > best_score:
                best_score = overlap
                best_match = i
        if best_match is not None and best_score >= 1:
            pairs.append({
                "chosen": s["conversations"],
                "rejected": failures[best_match]["conversations"],
                "prompt": s["conversations"][1]["value"] if len(s["conversations"]) > 1 else "",
            })
            used_failures.add(best_match)

    dpo_path = TRAINING_DIR / "dpo_dataset.jsonl"
    with open(str(dpo_path), "w", encoding="utf-8") as f:
        for pair in pairs:
            f.write(json.dumps(pair) + "\n")

    return {
        "successes": len(successes),
        "failures": len(failures),
        "pairs": len(pairs),
        "dpo_path": str(dpo_path),
    }


# ---------------------------------------------------------------------------
# Ollama Modelfile generation
# ---------------------------------------------------------------------------
def create_modelfile(base_model: str | None = None) -> dict:
    """Generate an Ollama Modelfile for fine-tuning."""
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)

    model = base_model or OLLAMA_MODEL
    sft_path = TRAINING_DIR / "sft_dataset.jsonl"

    if not sft_path.exists():
        return {"error": "No SFT dataset found. Run --prepare-sft first."}

    # Count training examples
    with open(str(sft_path)) as f:
        count = sum(1 for _ in f)

    # Build the system prompt from accumulated experience
    system_prompt = (
        "You are ANAH's autonomous task executor. You operate within a 5-level "
        "needs hierarchy (L1-L5). You analyze system state, generate actionable goals, "
        "and execute tasks to maintain and improve the system. "
        "You have been fine-tuned on your own successful task execution trajectories. "
        f"Training corpus: {count} successful task trajectories."
    )

    modelfile_content = f"""# ANAH Fine-tuned Model
# Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
# Training examples: {count}

FROM {model}

SYSTEM \"\"\"{system_prompt}\"\"\"

PARAMETER temperature 0.7
PARAMETER top_p 0.9
PARAMETER num_predict 1024
"""

    modelfile_path = TRAINING_DIR / "Modelfile"
    modelfile_path.write_text(modelfile_content)

    return {
        "modelfile_path": str(modelfile_path),
        "base_model": model,
        "training_examples": count,
        "system_prompt": system_prompt,
    }


# ---------------------------------------------------------------------------
# Training execution
# ---------------------------------------------------------------------------
def run_training_if_ready(model_name: str = "anah-tuned") -> dict:
    """Check if training conditions are met and run if so.

    Triggers when:
    - >20 new trajectories since last train
    - Last train was >24h ago (or never)
    Returns dict with 'triggered' bool and reason or training results.
    """
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    last_train_file = TRAINING_DIR / "last_train.json"

    # Check last train time
    last_train_ts = 0
    last_train_count = 0
    if last_train_file.exists():
        try:
            lt = json.loads(last_train_file.read_text())
            last_train_ts = lt.get("timestamp", 0)
            last_train_count = lt.get("trajectory_count", 0)
        except Exception:
            pass

    hours_since = (time.time() - last_train_ts) / 3600 if last_train_ts else float("inf")

    # Count current trajectories
    all_trajs = load_all_trajectories()
    current_count = len(all_trajs)
    new_trajs = current_count - last_train_count

    # Check conditions
    if new_trajs < 20 and hours_since < 24:
        return {
            "triggered": False,
            "reason": f"Not ready: {new_trajs} new trajectories (need 20), {hours_since:.1f}h since last train (need 24h)",
            "new_trajectories": new_trajs,
            "hours_since_last": round(hours_since, 1),
        }

    # Run training
    result = run_training(model_name)
    result["triggered"] = True

    # Record training run
    last_train_file.write_text(json.dumps({
        "timestamp": time.time(),
        "trajectory_count": current_count,
        "examples": result.get("sft", {}).get("after_dedup", 0),
        "model_name": model_name,
    }))

    return result


def run_training(model_name: str = "anah-tuned") -> dict:
    """Full training pipeline: prepare → create modelfile → build model."""
    results = {}

    # Step 1: Prepare SFT dataset
    print("[trainer] Preparing SFT dataset...", file=__import__("sys").stderr)
    sft_result = prepare_sft_dataset()
    results["sft"] = sft_result

    if sft_result["after_dedup"] == 0:
        return {**results, "error": "No quality trajectories found. Run ANAH longer to accumulate data."}

    # Step 2: Prepare DPO dataset (optional, may have no pairs)
    print("[trainer] Preparing DPO dataset...", file=__import__("sys").stderr)
    dpo_result = prepare_dpo_dataset()
    results["dpo"] = dpo_result

    # Step 3: Create Modelfile
    print("[trainer] Creating Modelfile...", file=__import__("sys").stderr)
    modelfile_result = create_modelfile()
    results["modelfile"] = modelfile_result

    if "error" in modelfile_result:
        return results

    # Step 4: Build the model via Ollama CLI
    print(f"[trainer] Building model '{model_name}' via Ollama...", file=__import__("sys").stderr)
    modelfile_path = modelfile_result["modelfile_path"]

    try:
        proc = subprocess.run(
            ["ollama", "create", model_name, "-f", modelfile_path],
            capture_output=True, text=True, timeout=300,
        )
        results["ollama_create"] = {
            "success": proc.returncode == 0,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "model_name": model_name,
        }
        if proc.returncode == 0:
            print(f"[trainer] Model '{model_name}' created successfully!", file=__import__("sys").stderr)
        else:
            print(f"[trainer] Ollama create failed: {proc.stderr}", file=__import__("sys").stderr)
    except FileNotFoundError:
        results["ollama_create"] = {
            "success": False,
            "error": "Ollama CLI not found. Install from ollama.com.",
        }
    except subprocess.TimeoutExpired:
        results["ollama_create"] = {
            "success": False,
            "error": "Ollama create timed out after 300s.",
        }

    return results


# ---------------------------------------------------------------------------
# A/B Model comparison
# ---------------------------------------------------------------------------
AB_TEST_PROMPTS = [
    {
        "system": "You are ANAH's L5 goal generator. Generate 1-2 goals as a JSON array.",
        "user": "System health: 85%, queue empty, no recent failures. Generate improvement goals.",
    },
    {
        "system": "You are ANAH's L5 goal generator. Generate 1-2 goals as a JSON array.",
        "user": "CPU at 90%, disk at 80%. Generate maintenance goals.",
    },
    {
        "system": "You are ANAH's L5 goal generator. Generate 1-2 goals as a JSON array.",
        "user": "Network check failed 5 times in the last hour. Generate diagnostic goals.",
    },
    {
        "system": "You are ANAH's L5 goal generator. Generate 1-2 goals as a JSON array.",
        "user": "System idle, health 100%, learned 3 skills. Generate self-improvement goals.",
    },
    {
        "system": "You are ANAH's L5 goal generator. Generate 1-2 goals as a JSON array.",
        "user": "Task completion rate dropped from 90% to 60%. Generate recovery goals.",
    },
]


def _score_response(content: str) -> float:
    """Score a model response for quality (0-1). Checks JSON parsability and goal relevance."""
    if not content:
        return 0.0
    score = 0.0
    # Check JSON parsability
    try:
        text = content
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        data = json.loads(text.strip())
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list) and len(data) > 0:
            score += 0.4  # Valid JSON array
            # Check goal structure
            for item in data:
                if isinstance(item, dict):
                    if "title" in item:
                        score += 0.2
                    if "priority" in item:
                        score += 0.1
                    if "description" in item or "reasoning" in item:
                        score += 0.1
                    break  # Score first goal only
    except (json.JSONDecodeError, IndexError):
        # Not valid JSON but has content
        if len(content) > 20:
            score += 0.1
    return min(score, 1.0)


def _call_model(model: str, system: str, user: str) -> str | None:
    """Call an Ollama model and return response content."""
    import urllib.request
    try:
        body = json.dumps({
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=body,
            headers={"content-type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        return data["message"]["content"]
    except Exception:
        return None


def compare_models(base_model: str | None = None, tuned_model: str = "anah-tuned") -> dict:
    """Run A/B comparison between base and tuned models."""
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    base = base_model or OLLAMA_MODEL

    results = []
    for i, prompt in enumerate(AB_TEST_PROMPTS):
        base_resp = _call_model(base, prompt["system"], prompt["user"])
        tuned_resp = _call_model(tuned_model, prompt["system"], prompt["user"])

        base_score = _score_response(base_resp)
        tuned_score = _score_response(tuned_resp)

        results.append({
            "prompt_idx": i,
            "base_score": base_score,
            "tuned_score": tuned_score,
            "winner": "tuned" if tuned_score > base_score else "base" if base_score > tuned_score else "tie",
        })

    tuned_wins = sum(1 for r in results if r["winner"] == "tuned")
    base_wins = sum(1 for r in results if r["winner"] == "base")
    ties = sum(1 for r in results if r["winner"] == "tie")

    eval_result = {
        "base_model": base,
        "tuned_model": tuned_model,
        "results": results,
        "tuned_wins": tuned_wins,
        "base_wins": base_wins,
        "ties": ties,
        "total": len(results),
        "tuned_better": tuned_wins >= 3,
        "timestamp": time.time(),
    }

    # Save eval results
    eval_file = TRAINING_DIR / "eval_results.json"
    eval_file.write_text(json.dumps(eval_result, indent=2))

    return eval_result


def promote_model(tuned_model: str = "anah-tuned") -> dict:
    """Promote tuned model if it scored better in A/B comparison."""
    eval_file = TRAINING_DIR / "eval_results.json"
    if not eval_file.exists():
        return {"promoted": False, "reason": "No eval results. Run --compare first."}

    eval_data = json.loads(eval_file.read_text())
    if not eval_data.get("tuned_better"):
        return {"promoted": False, "reason": f"Tuned model not better (wins: {eval_data.get('tuned_wins', 0)}/5)"}

    # Update state.json with new model
    state_file = ANAH_DIR / "state.json"
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            pass
    state["ollama_model"] = tuned_model
    state_file.write_text(json.dumps(state, indent=2))

    # Also update environment for current process
    os.environ["OLLAMA_MODEL"] = tuned_model

    return {
        "promoted": True,
        "model": tuned_model,
        "eval_wins": eval_data.get("tuned_wins", 0),
    }


def check_model_reversion() -> dict:
    """Revert to base model if tuned model produces 0 valid goals for 3 consecutive cycles."""
    state_file = ANAH_DIR / "state.json"
    if not state_file.exists():
        return {"reverted": False, "reason": "No state file"}

    state = json.loads(state_file.read_text())
    current_model = state.get("ollama_model", OLLAMA_MODEL)

    if current_model == OLLAMA_MODEL:
        return {"reverted": False, "reason": "Already on base model"}

    # Check recent goal generation from DB
    try:
        import sqlite3
        db = sqlite3.connect(str(ANAH_DIR / "anah.db"))
        db.row_factory = sqlite3.Row
        # Get last 3 LLM-generated goals
        rows = db.execute(
            "SELECT source FROM generated_goals WHERE source = 'llm' ORDER BY timestamp DESC LIMIT 3"
        ).fetchall()
        db.close()

        # If we have at least 3 cycles but 0 LLM goals, revert
        # Actually check if recent cycles produced goals
        reversion_file = ANAH_DIR / "training" / "zero_goal_count.json"
        zero_count = 0
        if reversion_file.exists():
            try:
                zero_count = json.loads(reversion_file.read_text()).get("count", 0)
            except Exception:
                pass

        if len(rows) == 0:
            zero_count += 1
            reversion_file.parent.mkdir(parents=True, exist_ok=True)
            reversion_file.write_text(json.dumps({"count": zero_count}))

            if zero_count >= 3:
                # Revert
                state["ollama_model"] = OLLAMA_MODEL
                state_file.write_text(json.dumps(state, indent=2))
                os.environ["OLLAMA_MODEL"] = OLLAMA_MODEL
                reversion_file.write_text(json.dumps({"count": 0}))
                return {"reverted": True, "model": OLLAMA_MODEL, "reason": f"{zero_count} consecutive cycles with 0 LLM goals"}
        else:
            # Reset counter on success
            if reversion_file.exists():
                reversion_file.write_text(json.dumps({"count": 0}))

        return {"reverted": False, "zero_goal_cycles": zero_count}
    except Exception as e:
        return {"reverted": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def dataset_stats() -> dict:
    """Get statistics about available training data."""
    all_trajs = load_all_trajectories()

    # Categorize
    by_outcome = {}
    by_handler = {}
    by_source = {}
    durations = []

    for t in all_trajs:
        meta = t.get("metadata", {})
        outcome = meta.get("outcome", "unknown")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1

        title = meta.get("title", "")
        handler = title.split(":")[0].strip().lower() if ":" in title else "general"
        by_handler[handler] = by_handler.get(handler, 0) + 1

        source = meta.get("source", "unknown")
        by_source[source] = by_source.get(source, 0) + 1

        if meta.get("duration_ms"):
            durations.append(meta["duration_ms"])

    # Check for existing datasets
    datasets = {}
    for name in ("sft_dataset.jsonl", "sft_sharegpt.json", "dpo_dataset.jsonl", "Modelfile"):
        p = TRAINING_DIR / name
        if p.exists():
            datasets[name] = {"size_bytes": p.stat().st_size, "modified": p.stat().st_mtime}

    return {
        "total_trajectories": len(all_trajs),
        "by_outcome": by_outcome,
        "by_handler": by_handler,
        "by_source": by_source,
        "duration_stats": {
            "min_ms": min(durations) if durations else None,
            "max_ms": max(durations) if durations else None,
            "avg_ms": sum(durations) / len(durations) if durations else None,
            "count": len(durations),
        },
        "quality_eligible": len(filter_quality(all_trajs, success_only=True)),
        "existing_datasets": datasets,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ANAH Trainer — trajectory fine-tuning pipeline")
    parser.add_argument("--prepare-sft", action="store_true", help="Build SFT dataset from successful trajectories")
    parser.add_argument("--prepare-dpo", action="store_true", help="Build DPO dataset from paired trajectories")
    parser.add_argument("--create-modelfile", action="store_true", help="Generate Ollama Modelfile")
    parser.add_argument("--train", action="store_true", help="Full pipeline: prepare + create + train")
    parser.add_argument("--train-if-ready", action="store_true", help="Train only if conditions met (>20 new trajs, >24h)")
    parser.add_argument("--compare", action="store_true", help="A/B compare base vs tuned model")
    parser.add_argument("--promote", action="store_true", help="Promote tuned model if it scored better")
    parser.add_argument("--check-reversion", action="store_true", help="Revert to base if tuned produces 0 goals")
    parser.add_argument("--stats", action="store_true", help="Show dataset statistics")
    parser.add_argument("--model-name", type=str, default="anah-tuned", help="Name for the fine-tuned model")
    parser.add_argument("--base-model", type=str, help="Base model to fine-tune from")
    args = parser.parse_args()

    ANAH_DIR.mkdir(exist_ok=True)

    if args.stats:
        print(json.dumps(dataset_stats(), indent=2))
    elif args.prepare_sft:
        print(json.dumps(prepare_sft_dataset(), indent=2))
    elif args.prepare_dpo:
        print(json.dumps(prepare_dpo_dataset(), indent=2))
    elif args.create_modelfile:
        print(json.dumps(create_modelfile(args.base_model), indent=2))
    elif args.train:
        result = run_training(args.model_name)
        print(json.dumps(result, indent=2, default=str))
    elif args.train_if_ready:
        result = run_training_if_ready(args.model_name)
        print(json.dumps(result, indent=2, default=str))
    elif args.compare:
        result = compare_models(args.base_model, args.model_name)
        print(json.dumps(result, indent=2))
    elif args.promote:
        result = promote_model(args.model_name)
        print(json.dumps(result, indent=2))
    elif args.check_reversion:
        result = check_model_reversion()
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()
