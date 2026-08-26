#!/usr/bin/env python3
"""
Query structured logs for troubleshooting.

Usage:
  python3 scripts/query_logs.py --today                    # All logs from today
  python3 scripts/query_logs.py --task-id <id>             # Logs for one task
  python3 scripts/query_logs.py --level ERROR --last 1h    # Errors in last hour
  python3 scripts/query_logs.py --stage failed             # All failure records
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils import utils


def parse_args():
    p = argparse.ArgumentParser(description="Query MoneyPrinterTurbo logs")
    p.add_argument("--today", action="store_true", help="Show today's logs")
    p.add_argument("--yesterday", action="store_true", help="Show yesterday's logs")
    p.add_argument("--task-id", help="Filter by task_id")
    p.add_argument("--level", default="", help="Filter by level (ERROR, WARNING, INFO)")
    p.add_argument("--last", default="", help="Time range: 1h, 30m, 2d")
    p.add_argument("--limit", type=int, default=200, help="Max lines to show")
    p.add_argument("--grep", default="", help="Free-text search in log messages")
    return p.parse_args()


def main():
    args = parse_args()
    logs_dir = os.path.join(utils.storage_dir(), "logs")

    if not os.path.exists(logs_dir):
        print("No logs directory found at: " + logs_dir)
        sys.exit(1)

    # Determine which log files to read
    today = datetime.now()
    files = []
    if args.today:
        files.append(os.path.join(logs_dir, "app_" + today.strftime("%Y-%m-%d") + ".jsonl"))
    elif args.yesterday:
        d = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        files.append(os.path.join(logs_dir, "app_" + d + ".jsonl"))
    else:
        # Default: read today's file + any uncompressed recent ones
        for fname in sorted(os.listdir(logs_dir)):
            if fname.endswith(".jsonl"):
                files.append(os.path.join(logs_dir, fname))

    # Parse time filter
    min_time = None
    if args.last:
        num = int("".join(c for c in args.last if c.isdigit()) or "1")
        unit = "".join(c for c in args.last if c.isalpha()).lower()
        delta = timedelta(
            hours=num if "h" in unit else 0,
            minutes=num if "m" in unit else 0,
            days=num if "d" in unit else 0,
        )
        min_time = (datetime.now() - delta).isoformat()

    # Read and filter
    count = 0
    for fpath in files:
        if not os.path.exists(fpath):
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                if count >= args.limit:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Filters
                if args.task_id:
                    rid = (record.get("record", {}).get("extra", {}).get("request_id", "") or
                           record.get("text", ""))
                    if args.task_id not in str(rid) and args.task_id not in str(record):
                        continue
                if args.level:
                    if record.get("record", {}).get("level", {}).get("name", "").upper() != args.level.upper():
                        continue
                if min_time:
                    ts = record.get("record", {}).get("time", {}).get("repr", "") or ""
                    if ts and ts < min_time:
                        continue
                if args.grep:
                    text = record.get("text", "") + json.dumps(record.get("record", {}))
                    if args.grep.lower() not in text.lower():
                        continue

                # Print in human-readable format
                rec = record.get("record", {})
                ts = rec.get("time", {}).get("repr", "") or ""
                lvl = rec.get("level", {}).get("name", "") or ""
                msg = record.get("text", "")
                trace = rec.get("extra", {}).get("trace_id", "")
                location = "{}:{}".format(
                    rec.get("name", ""), rec.get("line", "")
                )
                print("{} | {:8} | {} | {} | {}".format(
                    ts, lvl, trace, location, msg
                ))
                count += 1

    if count == 0:
        print("No matching log records found.")
    else:
        print("\n--- {} record(s) shown ---".format(count))


if __name__ == "__main__":
    main()
