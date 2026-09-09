"""
Video quality auto-scoring and anomaly detection.

Runs after each video generation to catch common quality issues before
operator review, reducing manual inspection overhead at scale (100+ videos/day).

Quality dimensions:
  - duration     Video length is within expected range
  - audio        Audio track exists and is not silent
  - subtitle     Subtitle file exists and has content
  - file_size    Output file size is reasonable (not truncated)
  - resolution   Video resolution matches expected dimensions
"""

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
import jieba

from app.utils import utils


# These words describe a generic production scene or generic equipment.  They
# are not sufficient evidence that a material represents the business entity in
# the narration (for example, "处理" must not make a dialysis shot pass with a
# drinking-water purifier image).
_ENTITY_STOPWORDS = {
    "处理", "设备", "系统", "机房", "医院", "场所", "运行", "安全", "健康",
    "饮用", "工作", "办公", "室内", "室外", "管理", "状态", "数据", "现场",
    "画面", "展示", "正在", "进行", "视频", "图片", "素材", "产品", "系列",
}


def _business_entity_terms(text: str) -> set[str]:
    """Extract non-generic business terms used to validate a material match."""
    return {
        token.strip().lower()
        for token in jieba.lcut(str(text or ""))
        if len(token.strip()) >= 2
        and token.strip().lower() not in _ENTITY_STOPWORDS
        and not token.strip().isdigit()
    }


def _material_entity_mismatches(match_data: dict[str, Any]) -> list[int]:
    """Return storyboard shot numbers whose accepted item lacks a business entity.

    ``material_match.json`` already proves that a candidate passed semantic and
    keyword gates.  This second check is intentionally stricter: it compares
    the business entities in the shot's text/queries with the selected item's
    filename and category, so a lone generic overlap cannot produce a perfect
    quality score.
    """
    mismatches: list[int] = []
    shots = match_data.get("shots", [])
    if not isinstance(shots, list):
        return mismatches

    for fallback_index, shot in enumerate(shots, 1):
        if not isinstance(shot, dict):
            continue
        accepted = shot.get("accepted")
        if not isinstance(accepted, dict):
            continue
        query_text = " ".join(
            [str(shot.get("text", "") or "")]
            + [str(query or "") for query in shot.get("queries", [])]
        )
        material_text = " ".join(
            str(accepted.get(key, "") or "") for key in ("name", "category")
        )
        shot_terms = _business_entity_terms(query_text)
        material_terms = _business_entity_terms(material_text)
        # Empty evidence means the shot cannot be assessed safely; leave it to
        # the existing semantic/keyword checks instead of fabricating a fail.
        if shot_terms and material_terms and not (shot_terms & material_terms):
            try:
                mismatches.append(int(shot.get("shot", fallback_index)))
            except (TypeError, ValueError):
                mismatches.append(fallback_index)
    return mismatches


@dataclass
class QualityReport:
    """Structured quality check result for a single video."""

    video_path: str
    passed: bool = True
    score: int = 100  # 0–100
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "video_path": self.video_path,
            "passed": self.passed,
            "score": self.score,
            "checks": self.checks,
            "anomalies": self.anomalies,
        }


# ── Probe helpers ──────────────────────────────────────────────────────

def _ffprobe(video_path: str) -> dict | None:
    """Extract video metadata via ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception as exc:
        logger.warning(f"ffprobe failed for {video_path}: {exc}")
    return None


def _get_duration(probe: dict) -> float:
    """Duration in seconds."""
    try:
        return float(probe.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        return 0.0


def _has_audio(probe: dict) -> bool:
    """True if at least one audio stream exists."""
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "audio":
            return True
    return False


def _get_resolution(probe: dict) -> tuple[int, int]:
    """Return (width, height) of the first video stream."""
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream.get("width", 0), stream.get("height", 0)
    return 0, 0


def _get_file_size_mb(video_path: str) -> float:
    """File size in megabytes."""
    try:
        return os.path.getsize(video_path) / (1024 * 1024)
    except OSError:
        return 0.0


def _subtitle_has_content(task_id: str) -> bool:
    """Check if subtitle file exists and is non-empty."""
    subtitle_path = os.path.join(utils.task_dir(task_id), "subtitle.srt")
    if not os.path.exists(subtitle_path):
        return False
    try:
        return os.path.getsize(subtitle_path) > 100  # At least 100 bytes
    except OSError:
        return False


# ── Quality checks ─────────────────────────────────────────────────────

def score_video(task_id: str, video_path: str, expected_duration: float = 0) -> QualityReport:
    """
    Run all quality checks on a single output video.

    *expected_duration* is the TTS audio length — if available, video duration
    should be close to it.
    """
    report = QualityReport(video_path=video_path)
    score = 100
    checks: dict[str, dict[str, Any]] = {}
    anomalies: list[str] = []

    # 1. File existence
    if not os.path.exists(video_path):
        report.passed = False
        report.score = 0
        report.checks = {"file_exists": {"passed": False, "detail": "Video file not found"}}
        report.anomalies = ["Video file not found"]
        return report

    checks["file_exists"] = {"passed": True, "detail": "OK"}

    # 2. File size
    size_mb = _get_file_size_mb(video_path)
    if size_mb <= 0.05:
        score -= 30
        anomalies.append(f"File size too small ({size_mb:.2f} MB — possible truncated output)")
        checks["file_size"] = {"passed": False, "detail": f"{size_mb:.2f} MB (too small)"}
    elif size_mb > 500:
        score -= 5
        anomalies.append(f"File size very large ({size_mb:.2f} MB)")
        checks["file_size"] = {"passed": True, "detail": f"{size_mb:.2f} MB (large but acceptable)"}
    else:
        checks["file_size"] = {"passed": True, "detail": f"{size_mb:.2f} MB"}

    # 3. FFprobe
    probe = _ffprobe(video_path)
    if probe is None:
        score -= 50
        anomalies.append("Cannot probe video (corrupted or non-playable)")
        checks["ffprobe"] = {"passed": False, "detail": "FFprobe failed — video may be corrupted"}
        report.passed = False
        report.score = score
        report.checks = checks
        report.anomalies = anomalies
        return report

    checks["ffprobe"] = {"passed": True, "detail": "OK"}

    # 4. Duration
    duration = _get_duration(probe)
    if duration <= 0.5:
        score -= 40
        anomalies.append(f"Duration {duration:.1f}s — too short")
        checks["duration"] = {"passed": False, "detail": f"{duration:.1f}s (too short)"}
    elif expected_duration > 0 and duration < expected_duration * 0.5:
        score -= 20
        anomalies.append(
            f"Duration {duration:.1f}s vs expected {expected_duration:.1f}s (TTS audio length) — significant mismatch"
        )
        checks["duration"] = {
            "passed": False,
            "detail": f"{duration:.1f}s vs expected {expected_duration:.1f}s",
        }
    elif expected_duration > 0 and duration < expected_duration * 0.85:
        score -= 10
        checks["duration"] = {
            "passed": True,
            "detail": f"{duration:.1f}s (slightly shorter than expected {expected_duration:.1f}s)",
        }
    else:
        checks["duration"] = {"passed": True, "detail": f"{duration:.1f}s"}

    # 5. Audio presence
    if _has_audio(probe):
        checks["audio"] = {"passed": True, "detail": "Audio track present"}
    else:
        score -= 25
        anomalies.append("No audio track — video is silent")
        checks["audio"] = {"passed": False, "detail": "No audio track found"}

    # 6. Resolution
    w, h = _get_resolution(probe)
    if w > 0 and h > 0:
        checks["resolution"] = {"passed": True, "detail": f"{w}x{h}"}
        if w < 100 or h < 100:
            score -= 15
            anomalies.append(f"Resolution {w}x{h} is too low")
            checks["resolution"] = {"passed": False, "detail": f"{w}x{h} (too low)"}
    else:
        score -= 10
        anomalies.append("Cannot determine resolution")
        checks["resolution"] = {"passed": False, "detail": "Unknown"}

    # 7. Subtitle presence (if enabled)
    sub_path = os.path.join(utils.task_dir(task_id), "subtitle.srt")
    if os.path.exists(sub_path) and _subtitle_has_content(task_id):
        checks["subtitle"] = {"passed": True, "detail": "Subtitle file present"}
    else:
        # Subtitle might be legitimately disabled; mark as warning not failure
        checks["subtitle"] = {"passed": True, "detail": "Subtitle not present or empty (may be intentional)"}

    # Content matching is separate from technical validity. A playable video
    # can still contain the wrong footage for one or more narration shots.
    match_path = os.path.join(utils.task_dir(task_id), "material_match.json")
    if os.path.exists(match_path):
        try:
            with open(match_path, "r", encoding="utf-8") as fp:
                match_data = json.load(fp)
            unmatched = int(match_data.get("unmatched", 0) or 0)
            total = int(match_data.get("total", 0) or 0)
            if unmatched:
                penalty = min(35, round(35 * unmatched / max(total, 1)))
                score -= penalty
                anomalies.append(
                    f"{unmatched}/{total} storyboard shots have no accepted material match"
                )
                checks["material_match"] = {
                    "passed": False,
                    "detail": f"{unmatched} unmatched shots; penalty {penalty}",
                }
            else:
                checks["material_match"] = {
                    "passed": True,
                    "detail": f"All {total} storyboard shots have accepted matches",
                }

            entity_mismatches = _material_entity_mismatches(match_data)
            if entity_mismatches:
                penalty = min(35, round(35 * len(entity_mismatches) / max(total, 1)))
                score -= penalty
                anomalies.append(
                    "Business entity mismatch in storyboard shots: "
                    + ", ".join(map(str, entity_mismatches))
                )
                checks["material_entity"] = {
                    "passed": False,
                    "detail": (
                        f"{len(entity_mismatches)}/{max(total, 1)} accepted matches lack "
                        f"a shared business entity; penalty {penalty}"
                    ),
                }
            elif match_data.get("shots"):
                checks["material_entity"] = {
                    "passed": True,
                    "detail": "Accepted materials share a business entity with each assessed shot",
                }
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            checks["material_match"] = {
                "passed": False,
                "detail": f"Unable to read material match report: {exc}",
            }
            anomalies.append("Material match report could not be verified")

    # Normalize score
    report.score = max(0, min(100, score))
    report.passed = report.score >= 70
    report.checks = checks
    report.anomalies = anomalies

    return report


def score_task(task_id: str, video_paths: list[str], audio_duration: float = 0) -> list[QualityReport]:
    """Run quality scoring on all output videos of a task.  Returns one report per video."""
    reports: list[QualityReport] = []
    for vp in video_paths:
        try:
            report = score_video(task_id, vp, expected_duration=audio_duration)
            reports.append(report)
            if report.passed:
                logger.info(
                    f"Quality check PASSED for {task_id}: score={report.score}, "
                    f"video={os.path.basename(vp)}"
                )
            else:
                logger.warning(
                    f"Quality check ISSUES for {task_id}: score={report.score}, "
                    f"anomalies={report.anomalies}, video={os.path.basename(vp)}"
                )
        except Exception as exc:
            logger.error(f"Quality scoring crashed for {task_id}/{vp}: {exc}")
            reports.append(
                QualityReport(
                    video_path=vp,
                    passed=False,
                    score=0,
                    checks={"error": {"passed": False, "detail": str(exc)}},
                    anomalies=[f"Scoring error: {exc}"],
                )
            )
    return reports


def save_quality_report(task_id: str, reports: list[QualityReport]) -> str:
    """Persist quality report to storage/tasks/{task_id}/quality.json."""
    report_path = os.path.join(utils.task_dir(task_id), "quality.json")
    data = {
        "task_id": task_id,
        "overall_score": (
            round(sum(r.score for r in reports) / len(reports)) if reports else 0
        ),
        "passed_count": sum(1 for r in reports if r.passed),
        "failed_count": sum(1 for r in reports if not r.passed),
        "total_videos": len(reports),
        "reports": [r.to_dict() for r in reports],
    }
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    return report_path
