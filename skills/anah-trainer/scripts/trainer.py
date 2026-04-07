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
    else:
        parser.print_help()
