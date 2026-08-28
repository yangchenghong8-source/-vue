"""回归测试：本次验收修复的缺陷。

- BUG-1/2：前端 llm.ts 原请求 /api/v1/llm/*，后端真实路由不带 /llm。
  本文件确认后端 /api/v1/scripts、/api/v1/terms、/api/v1/social-metadata
  存在（无认证 401），且 /api/v1/llm/* 不存在（404）。
- BUG-4：后端 create_task 此前只持久化 user_id、未持久化 params，
  导致任务列表「主题」列取不到 video_subject。本文件确认 params 被持久化。

BUG-3（前端 submitGeneration 防重复）由前端 Vitest 测试覆盖。
"""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.asgi import app
from app.controllers.v1 import video as video_controller
from app.models.schema import TaskVideoRequest
from app.services import state as sm


# ── BUG-1/2：路由前缀 ─────────────────────────────────────────────

def test_llm_prefixed_routes_are_not_script_endpoints():
    """前端曾误调 /api/v1/llm/*，这些路径不应是脚本/术语/元数据的有效端点。"""
    client = TestClient(app)
    for path in (
        "/api/v1/llm/scripts",
        "/api/v1/llm/terms",
        "/api/v1/llm/social-metadata",
    ):
        resp = client.post(path, json={})
        assert resp.status_code != 200, f"{path} 不应是可用的端点，实际 {resp.status_code}"


def test_real_llm_routes_require_auth():
    """真实路由不带 /llm，未登录时 401（而非 404）。"""
    client = TestClient(app)
    assert client.post("/api/v1/scripts", json={}).status_code == 401
    assert client.post("/api/v1/terms", json={}).status_code == 401
    assert client.post("/api/v1/social-metadata", json={}).status_code == 401


# ── BUG-4：create_task 持久化 params ─────────────────────────────

def test_create_task_persists_params_in_state():
    """创建任务后，任务状态必须包含 params.video_subject。"""
    task_id = "bugfix-regression-task-1"
    body = TaskVideoRequest(video_subject="回归测试主题", video_source="local")

    try:
        with (
            patch.object(video_controller.utils, "get_uuid", return_value=task_id),
            patch.object(video_controller.base, "get_task_id", return_value="req-123"),
            patch.object(video_controller.task_manager, "add_task") as add_task,
        ):
            response = video_controller.create_task(
                MagicMock(), body, stop_at="audio"
            )

        assert response["status"] == 200
        stored = sm.state.get_task(task_id)
        assert stored is not None, "任务未写入状态"
        assert stored["params"]["video_subject"] == "回归测试主题"
        add_task.assert_called_once()
    finally:
        sm.state.delete_task(task_id)


# ── BUG-4（根因）：后台进度更新不得覆盖 params ─────────────────────

def test_progress_update_does_not_drop_params():
    """创建任务持久化 params 后，后续只更新 state/progress 的 update_task
    调用（不带 params）不得把 params 覆盖掉（MemoryState 必须合并而非替换）。"""
    task_id = "bugfix-regression-merge-1"
    body = TaskVideoRequest(video_subject="合并语义测试主题", video_source="local")

    try:
        with (
            patch.object(video_controller.utils, "get_uuid", return_value=task_id),
            patch.object(video_controller.base, "get_task_id", return_value="req-456"),
            patch.object(video_controller.task_manager, "add_task") as add_task,
        ):
            video_controller.create_task(MagicMock(), body, stop_at="audio")

        # 模拟后台任务管理器在 20% 进度时只更新 state/progress（不带 params）
        sm.state.update_task(task_id, state=4, progress=20)

        stored = sm.state.get_task(task_id)
        assert stored is not None, "任务状态丢失"
        assert stored["params"]["video_subject"] == "合并语义测试主题"
        assert stored["progress"] == 20
        assert stored["state"] == 4
    finally:
        sm.state.delete_task(task_id)


# ── BUG-5（视频无法播放的根因）：stream 接口鉴权需支持 cookie 回退 ──

def test_get_current_user_falls_back_to_cookie():
    """浏览器原生 <video>/<a> 标签请求不带 Authorization 头，只带 cookie；
    认证依赖必须能从 cookie 回退读取 token 并解析出用户。"""
    from fastapi import Request

    from app.auth import deps as auth_deps
    from app.auth.security import create_access_token

    token = create_access_token(12345)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/stream/some/final-1.mp4",
        "headers": [(b"cookie", f"mpt_access_token={token}".encode())],
        "query_string": b"",
    }
    request = Request(scope)

    mock_user = MagicMock()
    mock_user.status = 0
    mock_db = MagicMock()
    mock_db.get.return_value = mock_user

    user = auth_deps._get_current_user(request=request, token=None, db=mock_db)

    assert user is mock_user
    mock_db.get.assert_called_once_with(auth_deps.User, 12345)


def test_get_current_user_requires_token():
    """既无 Authorization 头也无 cookie 时，应抛出 401。"""
    from fastapi import Request

    from app.auth import deps as auth_deps
    from app.models.exception import HttpException

    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/stream/some/final-1.mp4",
        "headers": [],
        "query_string": b"",
    })

    try:
        auth_deps._get_current_user(request=request, token=None, db=MagicMock())
    except HttpException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("未鉴权请求应抛出 401")


# ── BUG-6：任务列表接口必须归一化视频路径 ─────────────────────────

def test_task_list_transforms_video_paths():
    """列表接口（get_all_tasks）必须对 videos/combined_videos 调用
    _task_file_to_uri 归一化路径，而不是直接返回容器内绝对路径
    /MoneyPrinterTurbo/storage/tasks/...；否则前端 taskFileRelativePath
    无法剥离前缀，导致 stream/download 404。"""
    task_id = "bugfix-regression-list-1"
    fake_task = {
        "task_id": task_id,
        "state": 1,
        "progress": 100,
        "videos": [f"/MoneyPrinterTurbo/storage/tasks/{task_id}/final-1.mp4"],
        "combined_videos": [f"/MoneyPrinterTurbo/storage/tasks/{task_id}/combined-1.mp4"],
    }
    user = MagicMock()
    user.id = 12345
    user.role = "user"

    def fake_to_uri(file, endpoint, task_dir, request_id):
        return f"norm:{file.rsplit('/', 1)[-1]}"

    with (
        patch.object(
            video_controller.sm.state, "get_all_tasks", return_value=([fake_task], 1)
        ),
        patch.object(video_controller, "_task_file_to_uri", side_effect=fake_to_uri),
    ):
        response = video_controller.get_all_tasks(
            MagicMock(), page=1, page_size=10, current_user=user
        )

    tasks = response["data"]["tasks"]
    assert tasks[0]["videos"] == ["norm:final-1.mp4"]
    assert tasks[0]["combined_videos"] == ["norm:combined-1.mp4"]
