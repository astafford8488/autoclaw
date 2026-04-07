---
name: anah-trainer
description: "ANAH training pipeline — converts task execution trajectories into fine-tuning datasets. Supports SFT (supervised), DPO (preference), and Ollama Modelfile generation. Triggers on phrases like 'train anah', 'prepare training data', 'fine-tune model', 'training stats', 'build training dataset'."
---

# ANAH Trainer — Self-Improvement Engine

Converts accumulated task execution trajectories into fine-tuning datasets, enabling ANAH to learn from its own experience.

## Pipeline

1. **Collect** — Trajectories accumulate in `~/.anah/trajectories/` as ShareGPT-format JSON
2. **Filter** — Quality filters: min duration, exclude trivial handlers, deduplicate
3. **Prepare** — Convert to SFT (successes) and DPO (success/failure pairs) datasets
4. **Train** — Generate Ollama Modelfile and create fine-tuned model

## Usage

```bash
# View training data statistics
python scripts/trainer.py --stats

# Prepare SFT dataset (successful trajectories)
python scripts/trainer.py --prepare-sft

# Prepare DPO dataset (paired success/failure)
python scripts/trainer.py --prepare-dpo

# Generate Ollama Modelfile
python scripts/trainer.py --create-modelfile

# Full pipeline: prepare + create + train
python scripts/trainer.py --train --model-name anah-tuned
```

## Output Files

| File | Format | Purpose |
|------|--------|---------|
| `training/sft_dataset.jsonl` | Chat JSONL | Ollama/OpenAI fine-tuning |
| `training/sft_sharegpt.json` | ShareGPT | General training tools |
| `training/dpo_dataset.jsonl` | DPO JSONL | Preference optimization |
| `training/Modelfile` | Ollama | Model creation spec |
