"""
Task checkpoint persistence and recovery.

Checkpoints are saved to storage/tasks/{task_id}/checkpoint.json at every
pipeline stage.  On startup, tasks still in PROCESSING are marked FAILED.
"""

import json
import os
from datetime import datetime

from loguru import logger

from app.models import const
from app.utils import utils

CHECKPOINT_FILE = "checkpoint.json"


def _path(task_id: str) -> str:
    return os.path.join(utils.task_dir(task_id), CHECKPOINT_FILE)


def save(task_id: str, stage: str, progress: int, **extra) -> str:
    """Persist a checkpoint snapshot.  Returns the file path."""
    cp_path = _path(task_id)
    existing: dict = {}
    if os.path.exists(cp_path):
        try:
            with open(cp_path, "r", encoding="utf-8") as f:
                existing = json.loads(f.read())
        except Exception:
            pass
    checkpoint = {
        **existing,
        "task_id": task_id,
        "stage": stage,
        "progress": progress,
        "updated_at": datetime.now().isoformat(),
    }
    checkpoint.update(extra)
    with open(cp_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(checkpoint, ensure_ascii=False, indent=2))
    return cp_path


def load(task_id: str) -> dict | None:
    """Load the last checkpoint for a task."""
    cp_path = _path(task_id)
    if not os.path.exists(cp_path):
        return None
    try:
        with open(cp_path, "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except Exception as exc:
        logger.warning(f"Failed to load checkpoint for {task_id}: {exc}")
        return None


def mark_failed(task_id: str, stage: str, error: str, progress: int = 0) -> str:
    """Persist failure info, preserving earlier progress."""
    cp_path = _path(task_id)
    existing: dict = {}
    if os.path.exists(cp_path):
        try:
            with open(cp_path, "r", encoding="utf-8") as f:
                existing = json.loads(f.read())
        except Exception:
            pass
    preserved = existing.get("progress", progress) if progress == 0 else progress
    checkpoint = {
        **existing,
        "task_id": task_id,
        "stage": f"failed:{stage}",
        "progress": preserved,
        "state": "failed",
        "error": error,
        "failed_at": datetime.now().isoformat(),
    }
    with open(cp_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(checkpoint, ensure_ascii=False, indent=2))
    return cp_path


def recover_stuck_tasks(state_service) -> int:
    """On startup, mark PROCESSING tasks with checkpoints as FAILED."""
    recovered = 0
    tasks_dir = utils.task_dir()
    if not os.path.exists(tasks_dir):
        return 0
    for task_id in sorted(os.listdir(tasks_dir)):
        task_dir = os.path.join(tasks_dir, task_id)
        if not os.path.isdir(task_dir):
            continue
        if not os.path.exists(os.path.join(task_dir, CHECKPOINT_FILE)):
            continue
        try:
            task = state_service.get_task(task_id)
            if not task:
                continue
            try:
                state = int(task.get("state"))
            except (TypeError, ValueError):
                continue
            if state != const.TASK_STATE_PROCESSING:
                continue
            cp = load(task_id)
            stage = cp.get("stage", "unknown") if cp else "unknown"
            progress = cp.get("progress", 0) if cp else 0
            error = f"任务在阶段「{stage}」因服务器重启而中断 (进度: {progress}%)"
            state_service.update_task(
                task_id,
                state=const.TASK_STATE_FAILED,
                progress=progress,
                failed_stage=f"recovered:{stage}",
                error=error,
            )
            mark_failed(task_id, stage, error, progress)
            recovered += 1
            logger.warning(
                f"Recovered stuck task {task_id} | stage={stage} | progress={progress}%"
            )
        except Exception as exc:
            logger.error(f"Failed to recover stuck task {task_id}: {exc}")
    if recovered:
        logger.info(f"Startup recovery: {recovered} task(s) marked failed")
    return recovered
