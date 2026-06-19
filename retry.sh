#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# retry.sh – Drain the retry queue for failed products
#
# Usage:
#   ./retry.sh             # process all failed products
#   ./retry.sh --no-proxy  # without proxy
#
# Reads retry_queue.json, attempts to re-scrape each failed product URL,
# and appends successful results to the appropriate category CSV.
# Products that fail again remain in the queue for the next retry pass.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║        🔁  Hornbach Retry Queue Runner                   ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Virtualenv ───────────────────────────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
fi

# ── Check queue ──────────────────────────────────────────────────────────────
RETRY_FILE="$SCRIPT_DIR/retry_queue.json"
if [ ! -f "$RETRY_FILE" ]; then
    echo -e "${GREEN}✅ No retry_queue.json found — nothing to retry.${NC}"
    exit 0
fi

RETRY_COUNT=$(python3 -c "
import json
q = json.load(open('retry_queue.json'))
retryable = [x for x in q if x.get('attempts', 0) < 5]
exhausted  = [x for x in q if x.get('attempts', 0) >= 5]
print(f'{len(retryable)} retryable, {len(exhausted)} exhausted (max attempts reached)')
" 2>/dev/null || echo "unknown")

echo -e "${CYAN}🔁 Queue status: ${RETRY_COUNT}${NC}"
echo ""
echo -e "${CYAN}▶  Starting retry pass…  (Ctrl+C to stop safely)${NC}"
echo ""

# ── Log file ─────────────────────────────────────────────────────────────────
mkdir -p "$SCRIPT_DIR/output/logs"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$SCRIPT_DIR/output/logs/retry_${TIMESTAMP}.log"

# ── Run ──────────────────────────────────────────────────────────────────────
python3 spec_scraper.py --retry "$@" 2>&1 | tee "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}${BOLD}✅ Retry pass complete!${NC}"
else
    echo -e "${YELLOW}⚠  Retry pass exited with code $EXIT_CODE${NC}"
fi

# ── Remaining queue ───────────────────────────────────────────────────────────
if [ -f "$RETRY_FILE" ]; then
    REMAINING=$(python3 -c "
import json
q = json.load(open('retry_queue.json'))
retryable = [x for x in q if x.get('attempts', 0) < 5]
print(len(retryable))
" 2>/dev/null || echo "?")
    if [ "$REMAINING" != "0" ] && [ "$REMAINING" != "?" ]; then
        echo -e "${YELLOW}🔁 ${REMAINING} items still in retry queue. Run ./retry.sh again.${NC}"
    else
        echo -e "${GREEN}✅ Retry queue is clear!${NC}"
    fi
fi

echo -e "${CYAN}📋 Log: ${LOG_FILE}${NC}"
echo ""
