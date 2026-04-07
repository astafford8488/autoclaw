#!/usr/bin/env bash
# ANAH Cron Registration — Register ANAH heartbeat jobs with Autoclaw Gateway
#
# Prerequisites:
#   1. Autoclaw gateway must be running (openclaw gateway start)
#   2. openclaw CLI must be built and available
#
# Usage:
#   ./register_cron.sh              # Register all ANAH cron jobs
#   ./register_cron.sh --remove     # Remove all ANAH cron jobs
#   ./register_cron.sh --status     # Check cron status

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
BRIDGE="$SCRIPT_DIR/cron_bridge.py"

# Detect openclaw CLI
OPENCLAW="${OPENCLAW_CLI:-openclaw}"
if ! command -v "$OPENCLAW" &>/dev/null; then
    echo "Error: openclaw CLI not found. Build with: cd autoclaw && pnpm install && pnpm build"
    echo "Or set OPENCLAW_CLI to the path of the built CLI."
    exit 1
fi

remove_jobs() {
    echo "Removing ANAH cron jobs..."
    for name in anah-heartbeat anah-watchdog anah-training; do
        "$OPENCLAW" cron delete --name "$name" 2>/dev/null && echo "  Removed $name" || echo "  $name not found"
    done
}

register_jobs() {
    echo "Registering ANAH cron jobs with Autoclaw Gateway..."
    echo ""

    # 1. Full heartbeat cycle — every 3 minutes
    echo "  [1/3] anah-heartbeat (every 3m, isolated agent turn)"
    "$OPENCLAW" cron add \
        --name "anah-heartbeat" \
        --description "ANAH full heartbeat cycle: brainstem → cerebellum → cortex → executor → hippocampus → trajectory export" \
        --every 3m \
        --session isolated \
        --message "Run the ANAH heartbeat cycle. Execute: python \"$BRIDGE\" heartbeat. Report the summary JSON." \
        --tools exec,read \
        --announce \
        --json

    echo ""

    # 2. L1 watchdog — every 30 seconds
    echo "  [2/3] anah-watchdog (every 30s, main session system event)"
    "$OPENCLAW" cron add \
        --name "anah-watchdog" \
        --description "ANAH L1 quick health check — operational survival monitor" \
        --every 30s \
        --session main \
        --system-event "anah:watchdog — Run: python \"$BRIDGE\" watchdog" \
        --wake next-heartbeat \
        --json

    echo ""

    # 3. Training pipeline — daily at 3 AM
    echo "  [3/3] anah-training (daily 3 AM, isolated agent turn)"
    "$OPENCLAW" cron add \
        --name "anah-training" \
        --description "ANAH training pipeline: prepare SFT/DPO datasets and update Modelfile" \
        --cron "0 3 * * *" \
        --session isolated \
        --message "Run ANAH training pipeline. Execute: python \"$BRIDGE\" train. Report results." \
        --tools exec,read \
        --announce \
        --json

    echo ""
    echo "Done. Verify with: $OPENCLAW cron list"
}

show_status() {
    "$OPENCLAW" cron list --json 2>/dev/null || echo "Gateway not running or cron not available"
}

# Parse args
case "${1:-}" in
    --remove)
        remove_jobs
        ;;
    --status)
        show_status
        ;;
    *)
        register_jobs
        ;;
esac
