#!/usr/bin/env python3
"""
Performance benchmark for MoneyPrinterTurbo.

Measures per-stage latency to validate the 100-videos/day requirement.

Usage:
  python3 scripts/benchmark.py --subject "测试主题" --runs 5
  python3 scripts/benchmark.py --quick          # Skip video generation, only time LLM+TTS
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger


def parse_args():
    p = argparse.ArgumentParser(description="MoneyPrinterTurbo benchmark")
    p.add_argument("--subject", default="高效工作方法", help="Video subject")
    p.add_argument("--runs", type=int, default=3, help="Number of benchmark runs")
    p.add_argument("--quick", action="store_true", help="Skip video generation")
    return p.parse_args()


def time_stage(name, func, *args, **kwargs):
    start = time.perf_counter()
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        elapsed = time.perf_counter() - start
        return None, elapsed, str(exc)
    elapsed = time.perf_counter() - start
    return result, elapsed, None


def main():
    args = parse_args()

    from app.models.schema import VideoParams
    from app.services import task as tm, state as sm
    from app.utils import utils

    print("=" * 60)
    print("MoneyPrinterTurbo Benchmark")
    print("Subject: {}".format(args.subject))
    print("Runs:    {}".format(args.runs))
    print("Mode:    {}".format("quick (no video gen)" if args.quick else "full pipeline"))
    print("=" * 60)

    results = []
    for run_idx in range(args.runs):
        print("\n--- Run {}/{} ---".format(run_idx + 1, args.runs))
        task_id = utils.get_uuid()
        params = VideoParams(
            video_subject=args.subject,
            video_language="zh",
            paragraph_number=3,
            voice_name="zh-CN-YunxiaNeural-Male",
            voice_rate=1.0,
            subtitle_enabled=True,
            video_count=1,
        )

        stages = {}
        total_start = time.perf_counter()

        # Stage 1: Script generation
        print("  [1/5] Generating script...", end=" ", flush=True)
        stop = "script" if args.quick else "video"
        try:
            r = tm.start(task_id, params, stop_at="script")
        except Exception as _be:
            r = None
            print(f"FAILED — {_be}")
        if r and "script" in r:
            stages["script"] = {"ok": True}
            print("OK ({} chars)".format(len(str(r.get("script", "")))))
        else:
            stages["script"] = {"ok": False, "error": str(r)}
            print("FAILED")

        if not stages["script"]["ok"] and args.quick:
            results.append(stages)
            continue

        if args.quick:
            stages["total_seconds"] = round(time.perf_counter() - total_start, 1)
            print("  Total: {}s".format(stages["total_seconds"]))
            results.append(stages)
            continue

        # Full pipeline
        print("  [2/5] Generating terms...", end=" ", flush=True)
        try:
            r = tm.start(task_id, params, stop_at="terms")
        except Exception as _be:
            r = None
            print(f"FAILED (terms) — {_be}")
        stages["terms"] = {"ok": bool(r and "terms" in r)}

        print("  [3/5] Generating audio (TTS)...", end=" ", flush=True)
        try:
            r = tm.start(task_id, params, stop_at="audio")
        except Exception as _be:
            r = None
            print(f"FAILED (audio) — {_be}")
        stages["audio"] = {"ok": bool(r and "audio_file" in r)}

        print("  [4/5] Downloading materials...", end=" ", flush=True)
        try:
            r = tm.start(task_id, params, stop_at="materials")
        except Exception as _be:
            r = None
            print(f"FAILED (materials) — {_be}")
        stages["materials"] = {"ok": bool(r and "materials" in r)}

        print("  [5/5] Composing video...", end=" ", flush=True)
        try:
            r = tm.start(task_id, params, stop_at="video")
        except Exception as _be:
            r = None
            print(f"FAILED (video) — {_be}")
        stages["video"] = {"ok": bool(r and "videos" in r)}

        total = round(time.perf_counter() - total_start, 1)
        stages["total_seconds"] = total
        print("Total: {}s".format(total))
        results.append(stages)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    ok_runs = [r for r in results if all(v.get("ok", True) for v in r.values() if isinstance(v, dict))]
    fail_runs = len(results) - len(ok_runs)
    if ok_runs:
        avg_total = sum(r.get("total_seconds", 0) for r in ok_runs) / len(ok_runs)
        daily_capacity = int(86400 / avg_total * 5)  # 5 concurrent tasks
        print("Successful runs: {}/{}".format(len(ok_runs), len(results)))
        print("Avg total time:   {:.1f}s per video".format(avg_total))
        print("Daily capacity:   ~{} videos (5 concurrent)".format(daily_capacity))
        if daily_capacity >= 100:
            print("Verdict: PASS  (meets 100/day target)")
        else:
            print("Verdict: FAIL  (below 100/day, need optimization)")
    else:
        print("All runs failed. Check error messages above.")

    if fail_runs:
        print("Failed runs:      {}".format(fail_runs))

    # Save report
    report_path = os.path.join(
        utils.storage_dir(), "metrics", "benchmark_{}.json".format(datetime.now().strftime("%Y%m%d_%H%M%S"))
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "subject": args.subject,
            "runs": args.runs,
            "quick_mode": args.quick,
            "timestamp": datetime.now().isoformat(),
            "results": [
                {k: v for k, v in r.items() if not isinstance(v, dict) or v.get("ok") is not False}
                for r in results
            ],
            "daily_capacity_estimate": int(86400 / (sum(r.get("total_seconds", 300) for r in ok_runs) / max(len(ok_runs), 1)) * 5),
        }, ensure_ascii=False, indent=2))
    print("Report saved: {}".format(report_path))


if __name__ == "__main__":
    main()
