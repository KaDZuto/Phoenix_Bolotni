#!/usr/bin/env bash
# Monitoring script to verify that tmp/ directory does not leak audio bytes between batches.
# Can be run via cron every 10-60 minutes:
# */15 * * * * /root/Phoenix_Bolotni_Collector/scripts/check_tmp_clean.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TMP_DIR="${PROJECT_ROOT}/tmp"

MAX_ALLOWED_FILES=5
MAX_AGE_MINUTES=15

if [ ! -d "$TMP_DIR" ]; then
    echo "OK: $TMP_DIR does not exist yet (clean)."
    exit 0
fi

# Count files older than MAX_AGE_MINUTES
OLD_FILES=$(find "$TMP_DIR" -type f -mmin +"$MAX_AGE_MINUTES" 2>/dev/null | wc -l)
TOTAL_FILES=$(find "$TMP_DIR" -type f 2>/dev/null | wc -l)
TOTAL_SIZE_KB=$(du -sk "$TMP_DIR" 2>/dev/null | cut -f1)

echo "=== Disk & Temp Storage Health Check ==="
echo "Path: $TMP_DIR"
echo "Total files: $TOTAL_FILES"
echo "Total size: ${TOTAL_SIZE_KB} KB"
echo "Files older than ${MAX_AGE_MINUTES} min: $OLD_FILES"

if [ "$OLD_FILES" -gt 0 ]; then
    echo "⚠️ WARNING: Found $OLD_FILES stale temporary files older than $MAX_AGE_MINUTES minutes!"
    echo "Purging stale files..."
    find "$TMP_DIR" -type f -mmin +"$MAX_AGE_MINUTES" -delete
    find "$TMP_DIR" -type d -empty -delete
    echo "Stale files purged."
    exit 1
elif [ "$TOTAL_FILES" -gt "$MAX_ALLOWED_FILES" ]; then
    echo "⚠️ NOTICE: $TOTAL_FILES files currently in tmp (processing active)."
    exit 0
else
    echo "✅ OK: Temporary directory is clean and zero-leak compliant."
    exit 0
fi
