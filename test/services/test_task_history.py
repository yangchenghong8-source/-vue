"""``app.services.task_history`` 单元测试。

覆盖重点是"API 重启后历史视频仍可播放"这条回归路径：状态推导、归属落盘与
回落、以及磁盘/运行时合并。
"""

import json
import os

import pytest

from app.models import const
from app.services import task_history
from app.utils import utils


@pytest.fixture
def tasks_root(tmp_path, monkeypatch):
    """把任务根目录指向 tmp_path，并保持 utils.task_dir 的原有语义。"""
    root = tmp_path / "tasks"
    root.mkdir()

    def fake_task_dir(sub_dir: str = "") -> str:
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)
    return root


def _make_task(tasks_root, task_id, *, files=(), owner=None, script=None, checkpoint=None):
    task_dir = tasks_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    for name in files:
        (task_dir / name).write_bytes(b"x")
    if owner is not None:
        (task_dir / task_history.OWNER_FILE).write_text(
            json.dumps({"task_id": task_id, "user_id": owner}), encoding="utf-8"
        )
    if script is not None:
        (task_dir / task_history.SCRIPT_FILE).write_text(
            json.dumps(script, ensure_ascii=False), encoding="utf-8"
        )
    if checkpoint is not None:
        (task_dir / task_history.CHECKPOINT_FILE).write_text(
            json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
        )
    return task_dir


# ---- 成片识别 --------------------------------------------------------------


def test_find_final_video_ignores_intermediate_files(tasks_root):
    """合成中间产物不能被当成完成标志。"""
    task_dir = _make_task(
        tasks_root,
        "t1",
        files=("combined-1.mp4", "temp-clip-1.mp4", "audio.mp3", "final-1.mp4"),
    )

    found = task_history.find_final_video(str(task_dir))

    assert os.path.basename(found) == "final-1.mp4"


def test_find_final_video_returns_empty_without_final(tasks_root):
    task_dir = _make_task(tasks_root, "t1", files=("combined-1.mp4", "audio.mp3"))

    assert task_history.find_final_video(str(task_dir)) == ""


def test_find_final_video_picks_lowest_index(tasks_root):
    task_dir = _make_task(tasks_root, "t1", files=("final-3.mp4", "final-1.mp4", "final-2.mp4"))

    found = task_history.find_final_video(str(task_dir))

    assert os.path.basename(found) == "final-1.mp4"


# ---- 归属落盘与回落 --------------------------------------------------------


def test_write_then_read_owner_roundtrip(tasks_root):
    task_history.write_owner("t1", "42")

    assert task_history.read_owner("t1") == "42"


def test_read_owner_returns_none_for_legacy_task(tasks_root):
    """auth 之前的任务没有归属记录。"""
    _make_task(tasks_root, "t1", files=("final-1.mp4",))

    assert task_history.read_owner("t1") is None


def test_read_owner_falls_back_to_script_params(tasks_root):
    _make_task(tasks_root, "t1", script={"params": {"user_id": 7}})

    assert task_history.read_owner("t1") == "7"


def test_write_owner_tolerates_non_string_subject(tasks_root):
    """归属落盘不能因为一个非预期类型就把任务创建带崩。

    ``video_subject`` 来自请求模型。若它不是字符串，json.dumps 会抛
    TypeError；这个异常绝不能冒泡到 create_task。
    """

    class Weird:
        video_subject = object()

    task_history.write_owner("t1", "42", Weird())

    assert task_history.read_owner("t1") == "42"


def test_write_owner_records_string_subject(tasks_root):
    class Params:
        video_subject = "主题"

    task_history.write_owner("t1", "42", Params())

    record = json.loads(
        (tasks_root / "t1" / task_history.OWNER_FILE).read_text(encoding="utf-8")
    )
    assert record["video_subject"] == "主题"


def test_owner_matches_legacy_task_is_admin_only():
    assert task_history.owner_matches(None, user_id=5, is_admin=True) is True
    assert task_history.owner_matches(None, user_id=5, is_admin=False) is False


def test_owner_matches_compares_as_string():
    assert task_history.owner_matches("5", user_id=5, is_admin=False) is True
    assert task_history.owner_matches("5", user_id=6, is_admin=False) is False


# ---- 从磁盘重建任务 --------------------------------------------------------


def test_load_disk_task_marks_complete_when_final_video_exists(tasks_root):
    _make_task(
        tasks_root,
        "t1",
        files=("final-1.mp4",),
        owner="42",
        script={"params": {"video_subject": "主题"}},
    )

    task = task_history.load_disk_task("t1")

    assert task["state"] == const.TASK_STATE_COMPLETE
    assert task["progress"] == 100
    assert task["user_id"] == "42"
    assert task["subject"] == "主题"
    assert [os.path.basename(v) for v in task["videos"]] == ["final-1.mp4"]


def test_load_disk_task_marks_failed_from_checkpoint(tasks_root):
    _make_task(
        tasks_root,
        "t1",
        checkpoint={"stage": "failed:audio", "state": "failed", "progress": 30,
                    "error": "boom"},
    )

    task = task_history.load_disk_task("t1")

    assert task["state"] == const.TASK_STATE_FAILED
    assert task["progress"] == 30
    assert task["error"] == "boom"


def test_load_disk_task_leaves_state_unknown_without_evidence(tasks_root):
    """既没成片也没失败标记时不臆断状态，且不对外展示。"""
    _make_task(tasks_root, "t1", files=("audio.mp3",))

    task = task_history.load_disk_task("t1")

    assert task["state"] is None
    assert task_history.is_presentable(task) is False


def test_load_disk_task_returns_none_for_missing_task(tasks_root):
    assert task_history.load_disk_task("nope") is None


def test_task_path_does_not_create_directory(tasks_root):
    """只读路径不能有建目录的副作用。

    ``utils.task_dir(task_id)`` 会 makedirs，若查询路径误用它，任何被猜测的
    task_id 都会在磁盘上留下空目录。
    """
    task_history.task_path("ghost")
    task_history.task_exists("ghost")
    task_history.load_disk_task("ghost")

    assert not (tasks_root / "ghost").exists()


# ---- 扫描与合并 ------------------------------------------------------------


def test_scan_tasks_filters_by_owner(tasks_root):
    _make_task(tasks_root, "mine", files=("final-1.mp4",), owner="42")
    _make_task(tasks_root, "theirs", files=("final-1.mp4",), owner="99")

    tasks = task_history.scan_tasks(user_id=42, is_admin=False)

    assert [t["task_id"] for t in tasks] == ["mine"]


def test_scan_tasks_admin_sees_legacy_tasks(tasks_root):
    _make_task(tasks_root, "legacy", files=("final-1.mp4",))

    assert [t["task_id"] for t in task_history.scan_tasks(user_id=1, is_admin=True)] == [
        "legacy"
    ]
    assert task_history.scan_tasks(user_id=1, is_admin=False) == []


def test_scan_tasks_skips_unpresentable_tasks(tasks_root):
    """状态推导不出来的目录不能进列表 —— 响应模型要求 state 是 int。"""
    _make_task(tasks_root, "done", files=("final-1.mp4",), owner="42")
    _make_task(tasks_root, "half", files=("audio.mp3",), owner="42")

    tasks = task_history.scan_tasks(user_id=42, is_admin=False)

    assert [t["task_id"] for t in tasks] == ["done"]


def test_scan_tasks_sorted_by_mtime_desc(tasks_root):
    older = _make_task(tasks_root, "older", files=("final-1.mp4",), owner="42")
    newer = _make_task(tasks_root, "newer", files=("final-1.mp4",), owner="42")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    tasks = task_history.scan_tasks(user_id=42, is_admin=False)

    assert [t["task_id"] for t in tasks] == ["newer", "older"]


def test_merge_runtime_overlays_disk_record():
    disk = [{"task_id": "t1", "state": const.TASK_STATE_COMPLETE, "progress": 100,
             "videos": ["/tasks/t1/final-1.mp4"], "mtime": 10}]
    runtime = [{"task_id": "t1", "state": const.TASK_STATE_PROCESSING, "progress": 55}]

    merged = task_history.merge_runtime(disk, runtime)

    assert len(merged) == 1
    assert merged[0]["state"] == const.TASK_STATE_PROCESSING
    assert merged[0]["progress"] == 55
    # 运行时状态还没写出成片时，磁盘上已发现的产物不能凭空消失
    assert merged[0]["videos"] == ["/tasks/t1/final-1.mp4"]


def test_merge_runtime_keeps_runtime_only_tasks():
    """本次进程新建、磁盘还没产物的任务也要出现在列表里。"""
    runtime = [{"task_id": "fresh", "state": const.TASK_STATE_PROCESSING, "progress": 5}]

    merged = task_history.merge_runtime([], runtime)

    assert [t["task_id"] for t in merged] == ["fresh"]


def test_merge_runtime_sorted_by_mtime_desc():
    disk = [
        {"task_id": "a", "state": const.TASK_STATE_COMPLETE, "mtime": 1},
        {"task_id": "b", "state": const.TASK_STATE_COMPLETE, "mtime": 3},
    ]

    merged = task_history.merge_runtime(disk, [])

    assert [t["task_id"] for t in merged] == ["b", "a"]
