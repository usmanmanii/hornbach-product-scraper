#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_scraper.sh – Hornbach Specification Scraper Runner
#
# Usage:
#   ./run_scraper.sh                         # scrape all categories
#   ./run_scraper.sh --category "Garten"     # single category
#   ./run_scraper.sh --pages 2               # limit pages (testing)
#   ./run_scraper.sh --no-proxy              # disable proxy
#   ./run_scraper.sh --reset                 # reset ALL state and re-scrape
#   ./run_scraper.sh --category "Garten" --reset  # reset one category
#
# The script will:
#   1. Activate venv automatically
#   2. Resume from where it left off (state.json)
#   3. Save logs to output/logs/spec_YYYYMMDD_HHMMSS.log
#   4. On Ctrl+C: flush CSVs and exit cleanly
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Colour

# ── Banner ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║        🦏  Hornbach Specification Scraper                ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Virtualenv ───────────────────────────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
    echo -e "${GREEN}✓ Activating virtual environment…${NC}"
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
else
    echo -e "${YELLOW}⚠ No venv found at $VENV_DIR${NC}"
    echo -e "${YELLOW}  Trying system Python. Consider running: python3 -m venv venv && pip install -r requirements.txt${NC}"
fi

# ── Output dirs ──────────────────────────────────────────────────────────────
mkdir -p "$SCRIPT_DIR/output/specs"
mkdir -p "$SCRIPT_DIR/output/logs"

# ── Log file ─────────────────────────────────────────────────────────────────
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$SCRIPT_DIR/output/logs/spec_${TIMESTAMP}.log"
echo -e "${CYAN}📋 Logging to: ${LOG_FILE}${NC}"
echo ""

# ── State info ───────────────────────────────────────────────────────────────
STATE_FILE="$SCRIPT_DIR/state.json"
RETRY_FILE="$SCRIPT_DIR/retry_queue.json"

if [ -f "$STATE_FILE" ]; then
    echo -e "${GREEN}📂 Found existing state.json — will resume from last position${NC}"
    # Show quick summary of state
    python3 - <<'EOF'
import json, sys
try:
    with open("state.json") as f:
        state = json.load(f)
    cats = state.get("categories", {})
    done = sum(1 for v in cats.values() if v.get("status") == "done")
    in_prog = sum(1 for v in cats.values() if v.get("status") == "in_progress")
    pending = sum(1 for v in cats.values() if v.get("status") == "pending")
    total_scraped = sum(v.get("products_scraped", 0) for v in cats.values())
    print(f"   Categories: {done} done / {in_prog} in-progress / {pending} pending")
    print(f"   Total products scraped so far: {total_scraped:,}")
    if state.get("last_updated"):
        print(f"   Last updated: {state['last_updated']}")
except Exception as e:
    print(f"   (Could not read state: {e})")
EOF
else
    echo -e "${YELLOW}📂 No state.json found — starting fresh${NC}"
fi

if [ -f "$RETRY_FILE" ]; then
    RETRY_COUNT=$(python3 -c "import json; q=json.load(open('retry_queue.json')); print(len(q))" 2>/dev/null || echo "?")
    echo -e "${YELLOW}🔁 Retry queue: ${RETRY_COUNT} items pending (run ./retry.sh to process)${NC}"
fi

echo ""
echo -e "${CYAN}▶  Starting scraper…  (Press Ctrl+C to pause — progress is saved)${NC}"
echo ""

# ── Run ──────────────────────────────────────────────────────────────────────
# Pass all script arguments directly to spec_scraper.py
python3 spec_scraper.py "$@" 2>&1 | tee "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}${BOLD}✅ Scraper finished successfully!${NC}"
else
    echo -e "${YELLOW}⚠  Scraper exited with code $EXIT_CODE (check log for details)${NC}"
fi

# ── Output summary ───────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}📁 Output files:${NC}"
if [ -d "$SCRIPT_DIR/output/specs" ]; then
    ls -lh "$SCRIPT_DIR/output/specs"/*.csv 2>/dev/null | awk '{print "   " $NF, $5}' || echo "   (no CSV files yet)"
fi

echo ""
echo -e "${CYAN}📋 Full log saved to: ${LOG_FILE}${NC}"

# ── Retry reminder ───────────────────────────────────────────────────────────
if [ -f "$RETRY_FILE" ]; then
    RETRY_COUNT=$(python3 -c "import json; q=json.load(open('retry_queue.json')); print(len([x for x in q if x.get('attempts',0)<5]))" 2>/dev/null || echo "?")
    if [ "$RETRY_COUNT" != "0" ] && [ "$RETRY_COUNT" != "?" ]; then
        echo ""
        echo -e "${YELLOW}🔁 ${RETRY_COUNT} failed products in retry queue.${NC}"
        echo -e "${YELLOW}   Run: ${BOLD}./retry.sh${NC}${YELLOW} to attempt re-scraping them.${NC}"
    fi
fi

echo ""
