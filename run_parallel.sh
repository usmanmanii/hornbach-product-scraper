#!/usr/bin/env bash
# =============================================================================
# run_parallel.sh – Run multiple spec_scraper.py instances in parallel
#                   to fetch all remaining Hornbach products FAST.
#
# This script launches one background process per category with a staggered
# 10-second start to avoid thundering-herd proxy bans.
#
# Usage:
#   ./run_parallel.sh                        # all categories, 1 process each
#   ./run_parallel.sh --workers 3            # run up to 3 categories at once
#   ./run_parallel.sh --no-proxy             # disable proxy (localhost testing)
#   ./run_parallel.sh --category "Garten"   # single category
#
# Logs: output/logs/parallel_YYYYMMDD_HHMMSS/<category>.log
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Defaults ─────────────────────────────────────────────────────────────────
MAX_WORKERS=3        # simultaneous scraper processes
STAGGER_SECS=15      # seconds between starting each instance
PROXY_FLAG=""
SINGLE_CATEGORY=""

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workers|-w)   MAX_WORKERS="$2"; shift 2 ;;
    --no-proxy)     PROXY_FLAG="--no-proxy"; shift ;;
    --category|-c)  SINGLE_CATEGORY="$2"; shift 2 ;;
    --stagger)      STAGGER_SECS="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; shift ;;
  esac
done

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║   🚀  Hornbach Parallel Scraper – $(date '+%H:%M:%S')                   ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Venv ─────────────────────────────────────────────────────────────────────
if [ -d "$SCRIPT_DIR/venv" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
fi

# ── Prepare log dir ──────────────────────────────────────────────────────────
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="$SCRIPT_DIR/output/logs/parallel_${TIMESTAMP}"
mkdir -p "$LOG_DIR"
echo -e "${CYAN}📋 Logs: ${LOG_DIR}/${NC}"
echo ""

# ── Get categories from product-info.csv ──────────────────────────────────────
if [ -n "$SINGLE_CATEGORY" ]; then
    CATEGORIES=("$SINGLE_CATEGORY")
else
    # Parse categories from product-info.csv (skip header lines)
    CATEGORIES=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && CATEGORIES+=("$line")
    done < <(python3 - <<'PYEOF'
import csv, sys
from pathlib import Path
csv_path = Path("product-info.csv")
categories = []
with open(csv_path, newline="", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    header_found = False
    for row in reader:
        if not row or len(row) < 2:
            continue
        if not header_found:
            if row[0].strip().lower() == "name":
                header_found = True
            continue
        name = row[0].strip()
        url  = row[1].strip()
        if name and url.startswith("http"):
            print(name)
PYEOF
    )
fi

echo -e "${CYAN}📋 Categories to process: ${#CATEGORIES[@]}${NC}"
for cat in "${CATEGORIES[@]}"; do
    echo "   • $cat"
done
echo ""
echo -e "${CYAN}⚙  Max parallel workers  : $MAX_WORKERS${NC}"
echo -e "${CYAN}⚙  Start stagger         : ${STAGGER_SECS}s between each${NC}"
echo ""

# ── Check for proxy ───────────────────────────────────────────────────────────
if [ -z "$PROXY_FLAG" ]; then
    PROXY_CFG=$(grep -E "^PROXY_URL=" "$SCRIPT_DIR/.env" 2>/dev/null | head -1 || true)
    if [ -z "$PROXY_CFG" ]; then
        echo -e "${YELLOW}⚠  WARNING: No PROXY_URL found in .env${NC}"
        echo -e "${YELLOW}   hornbach.de requires a German IP. Set PROXY_URL in .env!${NC}"
        echo ""
    else
        echo -e "${GREEN}✓ Proxy configured in .env${NC}"
        echo ""
    fi
fi

# ── Cleanup handler ───────────────────────────────────────────────────────────
PIDS=()
cleanup() {
    echo ""
    echo -e "${YELLOW}⛔ Stopping all scraper processes…${NC}"
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -SIGINT "$pid" 2>/dev/null || true
        fi
    done
    echo -e "${GREEN}✅ All processes signalled. CSVs will be flushed by each process.${NC}"
    exit 0
}
trap cleanup SIGINT SIGTERM

# ── Run scrapers ──────────────────────────────────────────────────────────────
running=0
completed=0
# declare -A PID_TO_CAT  # Bash 4+ only, removed for Bash 3.2

for cat in "${CATEGORIES[@]}"; do
    # Wait if we've hit max parallel workers
    while [ "${running}" -ge "${MAX_WORKERS}" ]; do
        # Check which workers have finished
        new_pids=()
        for pid in "${PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                new_pids+=("$pid")
            else
                eval cat_done="\$PID_TO_CAT_$pid"
                echo -e "${GREEN}  ✅ Finished: ${cat_done:-unknown}${NC}"
                ((completed++)) || true
                ((running--)) || true
                unset "PID_TO_CAT_$pid"
            fi
        done
        PIDS=("${new_pids[@]:-}")
        if [ "${running}" -ge "${MAX_WORKERS}" ]; then
            sleep 5
        fi
    done

    # Sanitise category name for log filename
    safe_name=$(echo "$cat" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g' | sed 's/__*/_/g' | sed 's/^_//;s/_$//')
    log_file="${LOG_DIR}/${safe_name}.log"

    echo -e "${CYAN}  ▶ Starting: ${cat}${NC}  (log: ${log_file##*/})"

    python3 "$SCRIPT_DIR/spec_scraper.py" \
        --category "$cat" \
        $PROXY_FLAG \
        >"$log_file" 2>&1 &

    new_pid=$!
    PIDS+=("$new_pid")
    eval "PID_TO_CAT_$new_pid=\"\$cat\""
    ((running++)) || true

    # Stagger starts to avoid hammering proxy / site
    sleep "$STAGGER_SECS"
done

# ── Wait for all remaining ─────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}⏳ Waiting for all processes to finish…${NC}"
for pid in "${PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
    eval cat_done="\$PID_TO_CAT_$pid"
    echo -e "${GREEN}  ✅ Finished: ${cat_done:-unknown}${NC}"
    ((completed++)) || true
    unset "PID_TO_CAT_$pid"
done

echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║   ✅  All ${completed} categories complete!                         ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}📋 Logs saved to: ${LOG_DIR}${NC}"
echo ""

# ── Progress summary ──────────────────────────────────────────────────────────
echo -e "${CYAN}📊 Progress summary:${NC}"
python3 scratch/compare_all.py
echo ""
