#!/bin/bash
# Cleanup old task directories and cached videos (>7 days)
# Intended to run via cron: 0 3 * * * /home/ta/111/scripts/cleanup-storage.sh

STORAGE_DIR="/home/ta/111/storage"
DAYS=7
LOG_FILE="/tmp/mpt-cleanup.log"

echo "[$(date)] Starting storage cleanup (older than $DAYS days)..." >> "$LOG_FILE"

# Clean old task directories
if [ -d "$STORAGE_DIR/tasks" ]; then
    find "$STORAGE_DIR/tasks" -maxdepth 1 -type d -mtime +$DAYS -exec rm -rf {} \;         -exec echo "[$(date)] Removed task: {}" >> "$LOG_FILE" \;
fi

# Clean old cached videos
if [ -d "$STORAGE_DIR/cache_videos" ]; then
    find "$STORAGE_DIR/cache_videos" -maxdepth 1 -type f -mtime +$DAYS -delete         -printf "[$(date)] Removed cached video: %p
" >> "$LOG_FILE" 2>/dev/null ||     find "$STORAGE_DIR/cache_videos" -maxdepth 1 -type f -mtime +$DAYS -exec rm -f {} \;         -exec echo "[$(date)] Removed cached video: {}" >> "$LOG_FILE" \;
fi

echo "[$(date)] Cleanup complete" >> "$LOG_FILE"
