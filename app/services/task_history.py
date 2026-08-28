"""磁盘任务历史 —— 以文件系统为任务历史的真相源。

任务产物落在 ``storage/tasks/{task_id}/`` 下，是唯一不会因进程重启而丢失的
记录。内存 / Redis 状态只承载"正在运行"的进度，重启即失效；因此历史任务的
列表与归属校验都必须能从磁盘重建，否则 API 一重启，已生成的视频就会因为
查不到任务记录而变成 404。

上游 MoneyPrinterTurbo 在 Streamlit 层做过同样的磁盘扫描，这里把它下移到
服务层，让 API 与 WebUI 共用同一套推导规则。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger

from app.models import const
from app.utils import utils

# 只认最终成片。合成过程会留下 combined-* / temp-clip-* 等中间文件，
# 它们的存在不代表任务成功完成。
_FINAL_VIDEO_PATTERN = re.compile(
    r"final-(?P<index>\d+)\.(?P<ext>mp4|mov|mkv|avi|webm)$", re.IGNORECASE
)

OWNER_FILE = "task.json"
SCRIPT_FILE = "script.json"
CHECKPOINT_FILE = "checkpoint.json"

_SUBJECT_FALLBACK_LENGTH = 40


def task_path(task_id: str) -> str:
    """返回任务目录路径，且**不创建目录**。

    注意不能用 ``utils.task_dir(task_id)``：它会 ``makedirs``，在只读的查询
    路径上会为不存在的 task_id 凭空建出空目录。
    """
    return os.path.join(utils.task_dir(), task_id)


def task_exists(task_id: str) -> bool:
    """任务目录是否真实存在。"""
    if not task_id:
        return False
    return os.path.isdir(task_path(task_id))


def _read_json(file_path: str) -> dict[str, Any]:
    """尽力读取 JSON；损坏或不可读时返回空字典而不是抛异常。"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        logger.debug(f"skip unreadable json: {file_path}, {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def find_final_video(task_dir_path: str) -> str:
    """在任务目录里找序号最小的最终成片，找不到返回空串。"""
    try:
        files = os.listdir(task_dir_path)
    except OSError as exc:
        logger.debug(f"skip unavailable task directory: {task_dir_path}, {exc}")
        return ""

    candidates = [
        (int(match.group("index")), name)
        for name in files
        if (match := _FINAL_VIDEO_PATTERN.fullmatch(name))
    ]
    if not candidates:
        return ""

    _, file_name = min(candidates, key=lambda item: item[0])
    return os.path.join(task_dir_path, file_name)


# ---- 归属信息落盘 ----------------------------------------------------------
# 内存状态里的 user_id 会随进程消失。任务创建时立刻把归属写进任务目录，
# 这样重启后仍能判断视频归谁，多租户隔离不会退化成"谁都看不到"。


def write_owner(task_id: str, user_id: str | None, params: Any = None) -> str:
    """把任务归属写入 ``task.json``，返回文件路径。

    在任务**创建时**调用，而不是等 ``script.json`` 生成 —— 后者要等文案生成
    完成才落盘，任务早期失败就没有归属记录了。
    """
    owner_path = os.path.join(utils.task_dir(task_id), OWNER_FILE)
    record: dict[str, Any] = {"task_id": task_id, "user_id": user_id}
    if params is not None:
        subject = getattr(params, "video_subject", None)
        # 只接受真正的字符串：这个值来自请求模型，不能因为一个非预期类型就让
        # json.dumps 抛异常、把任务创建整条链路带崩。
        if isinstance(subject, str) and subject:
            record["video_subject"] = subject
    try:
        with open(owner_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, indent=2))
    except (OSError, TypeError, ValueError) as exc:
        # 归属落盘失败不应阻断任务创建：内存状态仍然可用，只是重启后退化为
        # legacy 任务（仅 admin 可见）。这里必须留下日志，避免静默降级。
        logger.error(f"failed to persist task owner: task_id={task_id}, error={exc}")
    return owner_path


def read_owner(task_id: str) -> str | None:
    """读取任务归属 user_id；无记录（历史任务）返回 None。"""
    owner_record = _read_json(os.path.join(task_path(task_id), OWNER_FILE))
    if owner_record:
        user_id = owner_record.get("user_id")
        if user_id is not None:
            return str(user_id)
        return None

    # 兼容：早期版本可能把归属塞在 script.json 的 params 里。
    script_params = _read_json(os.path.join(task_path(task_id), SCRIPT_FILE)).get(
        "params", {}
    )
    if isinstance(script_params, dict):
        user_id = script_params.get("user_id")
        if user_id is not None:
            return str(user_id)
    return None


def owner_matches(owner: str | None, user_id: Any, is_admin: bool) -> bool:
    """归属判定，与 ``state._task_visible`` 保持同一套语义。

    无归属记录的任务视为 auth 之前的 legacy 数据，仅 admin 可见。
    """
    if owner is None:
        return is_admin
    return str(owner) == str(user_id)


# ---- 从磁盘重建任务记录 ----------------------------------------------------


def _derive_state(final_video: str, checkpoint: dict[str, Any]) -> int | None:
    """由磁盘产物推导任务状态。

    有最终成片就是完成。没有成片时，只在 checkpoint 已显式标记失败的情况下
    判为失败；其余一律返回 None（未知），交给 runtime 覆盖层修正 —— 不臆断
    一个可能正在运行的任务已经失败。
    """
    if final_video:
        return const.TASK_STATE_COMPLETE
    if checkpoint.get("state") == "failed":
        return const.TASK_STATE_FAILED
    return None


def load_disk_task(task_id: str) -> dict[str, Any] | None:
    """从磁盘重建单个任务记录，目录不存在返回 None。"""
    if not task_exists(task_id):
        return None

    dir_path = task_path(task_id)
    script_data = _read_json(os.path.join(dir_path, SCRIPT_FILE))
    params = script_data.get("params")
    params = params if isinstance(params, dict) else {}
    checkpoint = _read_json(os.path.join(dir_path, CHECKPOINT_FILE))
    final_video = find_final_video(dir_path)

    subject = (
        params.get("video_subject")
        or checkpoint.get("video_subject")
        or str(script_data.get("script", ""))[:_SUBJECT_FALLBACK_LENGTH]
        or task_id
    )

    task: dict[str, Any] = {
        "task_id": task_id,
        "user_id": read_owner(task_id),
        "params": params,
        "subject": subject,
        "state": _derive_state(final_video, checkpoint),
        "progress": 100 if final_video else int(checkpoint.get("progress", 0) or 0),
        "source": "history",
    }
    if final_video:
        task["videos"] = [final_video]
    if checkpoint.get("stage"):
        task["stage"] = checkpoint["stage"]
    if checkpoint.get("error"):
        task["error"] = checkpoint["error"]
    try:
        task["mtime"] = os.path.getmtime(dir_path)
    except OSError:
        task["mtime"] = 0
    return task


def is_presentable(task: dict[str, Any]) -> bool:
    """磁盘任务是否可以对外展示。

    ``state`` 推导不出来（既没成片、也没失败标记）的任务不对外展示：响应模型
    ``TaskStatusData.state`` 是必填 int，塞 None 会让整个列表校验失败；而随便
    编一个状态等于撒谎。这类目录通常是建了却没跑起来的任务，或测试遗留的固定
    装置。真正在运行的任务由 runtime 覆盖层带上状态，不受这里影响。
    """
    return task.get("state") is not None


def scan_tasks(user_id: Any = None, is_admin: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    """扫描任务目录，返回该用户可见的历史任务，按修改时间倒序。"""
    tasks_root = utils.task_dir()
    try:
        entries = [entry for entry in os.scandir(tasks_root) if entry.is_dir()]
    except OSError as exc:
        logger.warning(f"failed to scan task directory: {tasks_root}, {exc}")
        return []

    dated: list[tuple[float, str]] = []
    for entry in entries:
        try:
            dated.append((entry.stat(follow_symlinks=False).st_mtime, entry.name))
        except OSError as exc:
            # 扫描期间目录可能正被删除，跳过即可，不应让整个列表失败。
            logger.debug(f"skip unavailable task directory: {entry.path}, {exc}")

    dated.sort(reverse=True)

    tasks: list[dict[str, Any]] = []
    for _mtime, task_id in dated[:limit]:
        task = load_disk_task(task_id)
        if task is None or not is_presentable(task):
            continue
        if not owner_matches(task.get("user_id"), user_id, is_admin):
            continue
        tasks.append(task)
    return tasks


def merge_runtime(
    disk_tasks: list[dict[str, Any]], runtime_tasks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """磁盘记录为底，运行时状态为覆盖层。

    运行时状态更新（含实时进度、失败原因），但只在磁盘已有该任务、或该任务
    是本次进程新建时才出现。合并后按 mtime 倒序。
    """
    merged: dict[str, dict[str, Any]] = {
        task["task_id"]: task for task in disk_tasks if task.get("task_id")
    }

    for runtime_task in runtime_tasks:
        task_id = runtime_task.get("task_id")
        if not task_id:
            continue
        base_task = merged.get(task_id, {})
        combined = {**base_task, **runtime_task, "source": "runtime"}
        # 运行时状态可能还没写出成片，此时保留磁盘上已发现的产物路径，
        # 避免历史成片在覆盖后凭空消失。
        if not combined.get("videos") and base_task.get("videos"):
            combined["videos"] = base_task["videos"]
        if combined.get("state") is None and base_task.get("state") is not None:
            combined["state"] = base_task["state"]
        combined.setdefault("mtime", base_task.get("mtime", 0))
        merged[task_id] = combined

    return sorted(merged.values(), key=lambda item: item.get("mtime", 0), reverse=True)
