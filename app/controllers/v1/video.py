import glob
import os
import pathlib
import shutil
from typing import Optional, Union
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, Path, Query, Request, UploadFile
from fastapi.params import File
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger

from app.config import config
from app.controllers import base
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.controllers.manager.redis_manager import RedisTaskManager
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import (
    AudioRequest,
    BgmRetrieveResponse,
    BgmUploadResponse,
    SubtitleRequest,
    TaskDeletionResponse,
    TaskListResponse,
    TaskQueryRequest,
    TaskQueryResponse,
    TaskResponse,
    TaskVideoRequest,
    VideoMaterialUploadResponse,
    VideoMaterialRetrieveResponse
)
from app.auth.deps import _get_current_user
from app.auth.models import User
from app.services import bgm as bgm_service
from app.services import state as sm
from app.services import task as tm
from app.services import task_history
from app.utils import file_security, utils

# 认证依赖项
# router = new_router(dependencies=[Depends(base.verify_token)])
router = new_router()

_enable_redis = config.app.get("enable_redis", False)
_redis_host = config.app.get("redis_host", "localhost")
_redis_port = config.app.get("redis_port", 6379)
_redis_db = config.app.get("redis_db", 0)
_redis_password = config.app.get("redis_password", None)
_max_concurrent_tasks = config.app.get("max_concurrent_tasks", 5)
_max_queued_tasks = config.app.get("max_queued_tasks", 100)

redis_url = f"redis://:{_redis_password}@{_redis_host}:{_redis_port}/{_redis_db}"
# 根据配置选择合适的任务管理器
if _enable_redis:
    task_manager = RedisTaskManager(
        max_concurrent_tasks=_max_concurrent_tasks,
        redis_url=redis_url,
        max_queued_tasks=_max_queued_tasks,
    )
else:
    task_manager = InMemoryTaskManager(
        max_concurrent_tasks=_max_concurrent_tasks,
        max_queued_tasks=_max_queued_tasks,
    )


def _task_belongs_to(task: dict, user: User) -> bool:
    """Return True if ``task`` is visible to ``user``.

    Tasks created before auth carry no user_id and are treated as legacy: only
    the admin may see them. Otherwise the task owner must match the user.
    """
    owner = task.get("user_id")
    if owner is None:
        return user.role == "admin"
    return str(owner) == str(user.id)


def _require_owned_task(task_id: str, user: User) -> dict:
    """返回属于当前用户的任务，否则按 404 处理（复用 ``_task_belongs_to``）。

    与 ``_require_owned_task_file`` 同样带磁盘回落，否则重启后历史任务的查询、
    删除、重试全部失效。
    """
    task = sm.state.get_task(task_id)
    if task and _task_belongs_to(task, user):
        return task

    if task is None:
        disk_task = task_history.load_disk_task(task_id)
        if disk_task is not None and task_history.owner_matches(
            disk_task.get("user_id"), user.id, user.role == "admin"
        ):
            return disk_task

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{task_id}: task not found"
    )


def _require_owned_task_file(file_path: str, user: User, request_id: str) -> None:
    """校验 stream/download 相对路径第一段 task_id 的所有权。

    产物路径形如 ``{task_id}/final-1.mp4``（相对 tasks 根目录）。取第一段作为
    task_id 并复用 ``_task_belongs_to`` 做多租户隔离；找不到任务或不属于当前
    用户一律按 404 处理，避免用不同状态码泄露任务是否存在。

    内存 / Redis 状态查不到时**回落到磁盘**：状态是易失的（进程重启即清空），
    而任务目录不是。少了这层回落，重启后所有历史视频都会因为查不到任务记录
    而变成 404 —— 文件明明还在。
    """
    task_id = (file_path or "").replace("\\", "/").strip("/").split("/")[0]
    if not task_id:
        raise HttpException(
            task_id=request_id, status_code=404, message=f"{request_id}: file not found"
        )

    task = sm.state.get_task(task_id)
    if task:
        if not _task_belongs_to(task, user):
            raise HttpException(
                task_id=request_id,
                status_code=404,
                message=f"{request_id}: file not found",
            )
        return

    is_admin = user.role == "admin"
    if not task_history.task_exists(task_id) or not task_history.owner_matches(
        task_history.read_owner(task_id), user.id, is_admin
    ):
        raise HttpException(
            task_id=request_id, status_code=404, message=f"{request_id}: file not found"
        )


_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_VIDEO_MATERIAL_BYTES = 500 * 1024 * 1024
# 合并磁盘历史时一次性取出的运行时任务上限。运行时状态只承载"正在跑"的任务，
# 数量远小于磁盘历史，取一页足够覆盖。
_RUNTIME_TASK_SCAN_LIMIT = 200


def _remove_upload_file(file_path: str) -> None:
    """尽力清理上传残留文件，且不覆盖调用方正在处理的原始异常。"""
    if not file_path or not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
    except OSError as exc:
        logger.warning(
            f"failed to remove uploaded file: path={file_path}, error={str(exc)}"
        )


def _sanitize_upload_filename(filename: str, request_id: str) -> str:
    # 浏览器或客户端有时会附带目录信息，甚至可能夹带 ../ 这类穿越片段。
    # 这里只保留纯文件名，避免上传接口把文件写到目标目录之外。
    normalized_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not normalized_name or normalized_name in {".", ".."}:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: invalid filename",
        )
    return normalized_name


def _resolve_path_within_directory(base_dir: str, unsafe_path: str, request_id: str) -> str:
    try:
        return file_security.resolve_path_within_directory(base_dir, unsafe_path)
    except ValueError as exc:
        logger.warning(
            f"reject unsafe file path, request_id: {request_id}, path: {unsafe_path}, "
            f"error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=404 if str(exc) == "file does not exist" else 403,
            message=f"{request_id}: invalid file path",
        )


def _public_task_data(task: dict) -> dict:
    """复制任务状态并移除仅用于服务端进程协调的内部字段。"""
    public_task = dict(task)
    public_task.pop("cross_post_owner", None)
    return public_task


def _task_file_to_uri(file: str, endpoint: str, task_dir: str, request_id: str) -> str:
    if not isinstance(file, str):
        return file

    if file.startswith(("http://", "https://")):
        return file

    try:
        resolved_path = file_security.resolve_path_within_directory(task_dir, file)
    except ValueError as exc:
        # 任务状态理论上只应保存任务目录内的产物路径。这里不再继续拼接 URL，
        # 避免把异常路径包装成可访问链接；同时保留原值，便于排查历史脏数据。
        logger.warning(
            f"skip unsafe task output path, request_id: {request_id}, path: {file}, "
            f"error: {str(exc)}"
        )
        return file

    relative_path = os.path.relpath(resolved_path, task_dir).replace("\\", "/")
    uri_path = f"tasks/{relative_path}"
    if endpoint:
        return f"{endpoint.rstrip('/')}/{uri_path}"
    return f"/{uri_path}"


def _parse_byte_range(
    range_header: str | None, file_size: int, request_id: str
) -> tuple[int, int]:
    """解析单段 HTTP Range，并把无效或越界请求稳定转换成 416。"""
    if file_size <= 0:
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        )

    if not range_header:
        return 0, file_size - 1

    try:
        # 视频播放器这里只需要单段 bytes range。拒绝多段请求可以避免返回体
        # 与 Content-Range 不一致，也避免异常字符串落入 int() 产生 500。
        if not range_header.startswith("bytes=") or "," in range_header:
            raise ValueError("unsupported range format")
        start_text, end_text = range_header[6:].split("-", 1)
        if not start_text and not end_text:
            raise ValueError("empty range")

        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError("invalid suffix length")
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
            if start < 0 or start >= file_size or end < start:
                raise ValueError("range outside file")
            end = min(end, file_size - 1)
    except (TypeError, ValueError) as exc:
        logger.warning(
            f"reject invalid video range, request_id: {request_id}, "
            f"range: {range_header}, file_size: {file_size}, error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        ) from exc

    return start, end


@router.post("/videos", response_model=TaskResponse, summary="Generate a short video")
def create_video(
    background_tasks: BackgroundTasks,
    request: Request,
    body: TaskVideoRequest,
    current_user: User = Depends(_get_current_user),
):
    return create_task(request, body, stop_at="video", current_user=current_user)


@router.post("/subtitle", response_model=TaskResponse, summary="Generate subtitle only")
def create_subtitle(
    background_tasks: BackgroundTasks,
    request: Request,
    body: SubtitleRequest,
    current_user: User = Depends(_get_current_user),
):
    return create_task(request, body, stop_at="subtitle", current_user=current_user)


@router.post("/audio", response_model=TaskResponse, summary="Generate audio only")
def create_audio(
    background_tasks: BackgroundTasks,
    request: Request,
    body: AudioRequest,
    current_user: User = Depends(_get_current_user),
):
    return create_task(request, body, stop_at="audio", current_user=current_user)


def create_task(
    request: Request,
    body: Union[TaskVideoRequest, SubtitleRequest, AudioRequest],
    stop_at: str,
    current_user: User = None,
):
    task_id = utils.get_uuid()
    request_id = base.get_task_id(request)

    # Validate: video_subject must not be empty
    if hasattr(body, "video_subject") and (not body.video_subject or not body.video_subject.strip()):
        raise HttpException(
            task_id=task_id,
            status_code=400,
            message=f"{request_id}: video_subject must not be empty",
        )

    try:
        task = {
            "task_id": task_id,
            "request_id": request_id,
            "params": body.model_dump(),
        }
        if current_user is not None:
            task["user_id"] = str(current_user.id)
        sm.state.update_task(task_id, user_id=task.get("user_id"), params=task["params"])
        # 归属同时落盘：内存状态会随进程消失，磁盘记录不会。少了这一步，
        # 重启后所有任务都退化成无主的 legacy 任务（仅 admin 可见）。
        task_history.write_owner(task_id, task.get("user_id"), body)
        task_manager.add_task(tm.start, task_id=task_id, params=body, stop_at=stop_at)
        logger.success(f"Task created: {utils.to_json(task)}")
        return utils.get_response(200, task)
    except TaskQueueFullError as e:
        sm.state.delete_task(task_id)
        logger.warning(
            f"reject task because queue is full, request_id: {request_id}, task_id: {task_id}"
        )
        raise HttpException(
            task_id=task_id, status_code=429, message=f"{request_id}: {str(e)}"
        )
    except ValueError as e:
        raise HttpException(
            task_id=task_id, status_code=400, message=f"{request_id}: {str(e)}"
        )

@router.get("/tasks", response_model=TaskListResponse, summary="Get all tasks")
def get_all_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    current_user: User = Depends(_get_current_user),
):
    request_id = base.get_task_id(request)
    endpoint = config.app.get("endpoint", "").rstrip("/")
    task_dir = utils.task_dir()
    is_admin = current_user.role == "admin"

    # 磁盘为底、运行时状态为覆盖层。反过来（只读运行时状态）会导致进程重启后
    # 历史任务全部消失 —— 产物明明还在磁盘上。
    disk_tasks = task_history.scan_tasks(user_id=current_user.id, is_admin=is_admin)
    runtime_tasks, _runtime_total = sm.state.get_all_tasks(
        1,
        _RUNTIME_TASK_SCAN_LIMIT,
        user_id=current_user.id,
        is_admin=is_admin,
    )
    merged_tasks = task_history.merge_runtime(disk_tasks, runtime_tasks)

    total = len(merged_tasks)
    start = (page - 1) * page_size
    tasks = merged_tasks[start : start + page_size]

    def _task_to_public_data(task: dict) -> dict:
        public_task = _public_task_data(task)
        if "videos" in task:
            public_task["videos"] = [
                _task_file_to_uri(v, endpoint, task_dir, request_id)
                for v in task["videos"]
            ]
        if "combined_videos" in task:
            public_task["combined_videos"] = [
                _task_file_to_uri(v, endpoint, task_dir, request_id)
                for v in task["combined_videos"]
            ]
        return public_task

    response = {
        "tasks": [_task_to_public_data(task) for task in tasks],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    return utils.get_response(200, response)



@router.get(
    "/tasks/{task_id}", response_model=TaskQueryResponse, summary="Query task status"
)
def get_task(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    query: TaskQueryRequest = Depends(),
    current_user: User = Depends(_get_current_user),
):
    request_id = base.get_task_id(request)
    endpoint = config.app.get("endpoint", "").rstrip("/")
    task = sm.state.get_task(task_id)
    if task and not _task_belongs_to(task, current_user):
        task = None
    if task is None:
        # 运行时状态查不到时回落磁盘，让历史任务在重启后仍可查询、仍能拿到播放地址。
        disk_task = task_history.load_disk_task(task_id)
        if (
            disk_task is not None
            and task_history.is_presentable(disk_task)
            and task_history.owner_matches(
                disk_task.get("user_id"), current_user.id, current_user.role == "admin"
            )
        ):
            task = disk_task
    if task:
        task_dir = utils.task_dir()
        response_task = _public_task_data(task)

        if "videos" in task:
            response_task["videos"] = [
                _task_file_to_uri(v, endpoint, task_dir, request_id)
                for v in task["videos"]
            ]
        if "combined_videos" in task:
            response_task["combined_videos"] = [
                _task_file_to_uri(v, endpoint, task_dir, request_id)
                for v in task["combined_videos"]
            ]
        return utils.get_response(200, response_task)

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )


@router.delete(
    "/tasks/{task_id}",
    response_model=TaskDeletionResponse,
    summary="Delete a generated short video task",
)
def delete_video(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    current_user: User = Depends(_get_current_user),
):
    request_id = base.get_task_id(request)
    # 走带磁盘回落的解析。原先直接用 sm.state.get_task，而运行时状态是易失的
    # （enable_redis = false 时是进程内存，重启即清空），任务目录不是。于是任务
    # 列表（磁盘为底）能列出历史任务，删除却一律 404 —— 文件明明还在，界面上
    # 看得见、删不掉。查询与重试早已改用这个 helper，删除漏了。
    task = _require_owned_task(task_id, current_user)

    if tm.is_task_busy(task):
        logger.warning(
            f"refuse to delete busy task, request_id: {request_id}, "
            f"task_id: {task_id}, state: {task.get('state')}, "
            f"cross_post_state: {task.get('cross_post_state')}"
        )
        raise HttpException(
            task_id=task_id,
            status_code=409,
            message=f"{request_id}: task is still running",
        )

    tasks_dir = utils.task_dir()
    current_task_dir = os.path.join(tasks_dir, task_id)
    if os.path.exists(current_task_dir):
        shutil.rmtree(current_task_dir)

    sm.state.delete_task(task_id)
    logger.success(f"video deleted: task_id={task_id}")
    return utils.get_response(200)


@router.get(
    "/musics", response_model=BgmRetrieveResponse, summary="Retrieve local BGM files"
)
def get_bgm_list(
    request: Request,
    current_user: User = Depends(_get_current_user),
):
    bgm_list = []
    for file in bgm_service.list_bgm_files():
        filename = os.path.basename(file)
        bgm_list.append(
            {
                "name": filename,
                "size": os.path.getsize(file),
                # 只返回文件名，避免把服务器绝对路径暴露给调用方。服务端会
                # 在 storage/bgm 和 resource/songs 两个白名单目录中重新解析。
                "file": filename,
            }
        )
    response = {"files": bgm_list}
    return utils.get_response(200, response)


@router.post(
    "/musics",
    response_model=BgmUploadResponse,
    summary="Upload a background music file",
    description=(
        "Validate an MP3, M4A, AAC, WAV, FLAC, OGG, OPUS, or WMA file up to "
        "30 MB and store it under an immutable UUID filename in storage/bgm."
    ),
    responses={
        400: {"description": "The filename, format, size, or audio stream is invalid"},
        500: {"description": "FFmpeg validation or persistent storage is unavailable"},
    },
)
def upload_bgm_file(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(_get_current_user),
):
    request_id = base.get_task_id(request)
    try:
        safe_filename = bgm_service.save_bgm_upload(file.filename, file.file)
    except bgm_service.BgmUploadError as exc:
        # 上传失败通常可以由用户更换文件后恢复，因此记录 request_id 和明确原因，
        # 但不输出文件内容或绝对路径，避免日志泄露用户数据。
        logger.warning(
            f"background music upload rejected: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        )
    except bgm_service.BgmServiceError as exc:
        # 工具链或存储故障属于服务端问题，不能伪装成用户文件错误。日志保留
        # request_id 和内部原因，HTTP 响应只返回稳定文案，避免暴露服务器路径。
        logger.error(
            f"background music upload failed: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: background music validation is unavailable",
        )

    response = {"file": safe_filename}
    return utils.get_response(200, response)

@router.get(
    "/video_materials", response_model=VideoMaterialRetrieveResponse, summary="Retrieve local video materials"
)
def get_video_materials_list(
    request: Request,
    current_user: User = Depends(_get_current_user),
):
    allowed_suffixes = ("mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png")
    local_videos_dir = utils.storage_dir("local_videos", create=True)
    files = []
    for suffix in allowed_suffixes:
        files.extend(glob.glob(os.path.join(local_videos_dir, f"*.{suffix}")))
    # 文件系统枚举顺序不稳定，直接返回会导致“顺序拼接”在不同机器或不同
    # 时刻表现不一致。这里统一按文件名排序，至少保证服务端返回顺序可预测。
    files.sort(key=lambda file_path: os.path.basename(file_path).lower())
    video_materials_list = []
    for file in files:
        filename = os.path.basename(file)
        video_materials_list.append(
            {
                "name": filename,
                "size": os.path.getsize(file),
                # 与 BGM 一样，只返回文件名；创建任务时再在 local_videos
                # 白名单目录内解析，避免 API 泄露宿主机绝对路径。
                "file": filename,
            }
        )
    response = {"files": video_materials_list}
    return utils.get_response(200, response)


@router.post(
    "/video_materials",
    response_model=VideoMaterialUploadResponse,
    summary="Upload the video material file to the local videos directory",
)
def upload_video_material_file(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(_get_current_user),
):
    request_id = base.get_task_id(request)
    safe_filename = _sanitize_upload_filename(file.filename, request_id)
    allowed_suffixes = ("mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png")
    suffix = pathlib.Path(safe_filename).suffix.lower().lstrip(".")
    # 按完整扩展名校验，既兼容 .MOV 这类大写后缀，也避免 photojpg 这种没有
    # 点号的文件名因为 endswith("jpg") 被误当成合法图片。
    if suffix not in allowed_suffixes:
        raise HttpException(
            "",
            status_code=400,
            message=(
                f"{request_id}: Only files with extensions "
                f"{', '.join(allowed_suffixes)} can be uploaded"
            ),
        )

    local_videos_dir = utils.storage_dir("local_videos", create=True)
    # 使用 UUID 落盘：既避免同名文件互相覆盖（跨用户干扰），也让并发上传互不影响。
    stored_name = f"{uuid4().hex}.{suffix}"
    save_path = os.path.join(local_videos_dir, stored_name)

    try:
        file.file.seek(0)
        total_bytes = 0
        with open(save_path, "wb") as buffer:
            while True:
                chunk = file.file.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > _MAX_VIDEO_MATERIAL_BYTES:
                    raise HttpException(
                        task_id=request_id,
                        status_code=400,
                        message=f"{request_id}: video material exceeds the 500 MB limit",
                    )
                buffer.write(chunk)
    except HttpException:
        _remove_upload_file(save_path)
        raise
    except OSError:
        _remove_upload_file(save_path)
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: failed to store video material",
        )

    response = {"file": stored_name}
    return utils.get_response(200, response)

@router.get("/stream/{file_path:path}")
async def stream_video(
    request: Request,
    file_path: str,
    current_user: User = Depends(_get_current_user),
):
    request_id = base.get_task_id(request)
    _require_owned_task_file(file_path, current_user, request_id)
    tasks_dir = utils.task_dir()
    video_path = _resolve_path_within_directory(tasks_dir, file_path, request_id)
    range_header = request.headers.get("Range")
    video_size = os.path.getsize(video_path)
    start, end = _parse_byte_range(range_header, video_size, request_id)
    length = end - start + 1

    def file_iterator(file_path, offset=0, bytes_to_read=None):
        with open(file_path, "rb") as f:
            f.seek(offset, os.SEEK_SET)
            remaining = bytes_to_read or video_size
            while remaining > 0:
                bytes_to_read = min(4096, remaining)
                data = f.read(bytes_to_read)
                if not data:
                    break
                remaining -= len(data)
                yield data

    response = StreamingResponse(
        file_iterator(video_path, start, length), media_type="video/mp4"
    )
    response.headers["Content-Range"] = f"bytes {start}-{end}/{video_size}"
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    response.status_code = 206  # Partial Content

    return response


@router.get("/download/{file_path:path}")
async def download_video(
    request: Request,
    file_path: str,
    current_user: User = Depends(_get_current_user),
):
    """
    download video
    :param request: Request request
    :param file_path: video file path, eg: /cd1727ed-3473-42a2-a7da-4faafafec72b/final-1.mp4
    :return: video file
    """
    request_id = base.get_task_id(request)
    _require_owned_task_file(file_path, current_user, request_id)
    tasks_dir = utils.task_dir()
    video_path = _resolve_path_within_directory(tasks_dir, file_path, request_id)
    file_path = pathlib.Path(video_path)
    filename = file_path.stem
    extension = file_path.suffix
    headers = {"Content-Disposition": f"attachment; filename={filename}{extension}"}
    return FileResponse(
        path=video_path,
        headers=headers,
        filename=f"{filename}{extension}",
        media_type=f"video/{extension[1:]}",
    )


# ── Retry endpoints ───────────────────────────────────────────────────

@router.post("/videos/{task_id}/retry", response_model=TaskResponse, summary="Retry a failed task")
def retry_task(
    background_tasks: BackgroundTasks,
    request: Request,
    task_id: str = Path(..., description="Task ID to retry"),
    current_user: User = Depends(_get_current_user),
):
    """Re-submit a failed task with the same parameters read from script.json."""
    _require_owned_task(task_id, current_user)
    import json as _json
    task_dir = utils.task_dir(task_id)
    script_file = os.path.join(task_dir, "script.json")
    if not os.path.exists(script_file):
        raise HttpException(
            task_id=task_id, status_code=404,
            message=f"Task {task_id} not found or script data missing",
        )
    try:
        with open(script_file, "r", encoding="utf-8") as _f:
            script_data = _json.loads(_f.read())
    except Exception as _e:
        raise HttpException(
            task_id=task_id, status_code=400,
            message=f"Failed to read task script data: {_e}",
        )
    old_params = script_data.get("params", {})
    if not old_params:
        raise HttpException(
            task_id=task_id, status_code=400,
            message="Task has no saved parameters for retry",
        )
    from app.models.schema import TaskVideoRequest
    try:
        body = TaskVideoRequest(**old_params)
    except Exception as _e:
        raise HttpException(
            task_id=task_id, status_code=400,
            message=f"Failed to reconstruct task parameters: {_e}",
        )
    return create_task(request, body, stop_at="video", current_user=current_user)


# ── Quality scoring endpoints (Level 3.5) ─────────────────────────────

@router.get("/videos/{task_id}/quality", summary="Get video quality report")
def get_quality_report(
    task_id: str = Path(..., description="Task ID"),
    current_user: User = Depends(_get_current_user),
):
    """Return the quality-scoring report for a completed task."""
    _require_owned_task(task_id, current_user)
    import json as _json
    qr_path = os.path.join(utils.task_dir(task_id), "quality.json")
    if not os.path.exists(qr_path):
        raise HttpException(
            task_id=task_id, status_code=404,
            message="Quality report not found. The task may not be complete or quality scoring was not run.",
        )
    try:
        with open(qr_path, "r", encoding="utf-8") as _f:
            report = _json.loads(_f.read())
    except Exception as _e:
        raise HttpException(
            task_id=task_id, status_code=500,
            message=f"Failed to read quality report: {_e}",
        )
    return utils.get_response(200, report)


# ── Batch task generation (Level 3.4) ────────────────────────────────

from fastapi import File as _File, UploadFile as _UploadFile


@router.post("/videos/batch", summary="Batch create video tasks from CSV or JSON")
async def batch_create_tasks(
    request: Request,
    background_tasks: BackgroundTasks,
    csv_file: Optional[_UploadFile] = _File(None, description="CSV file with batch tasks"),
    json_body: Optional[str] = Query(None, description="JSON array of task objects (alternative to CSV)"),
    stop_at: str = Query("video", description="Pipeline stage to stop at"),
    current_user: User = Depends(_get_current_user),
):
    """
    Submit multiple video-generation tasks at once.

    Two input modes (mutually exclusive):
      1. Upload a CSV file via ``csv_file``
      2. Pass a JSON array via ``json_body`` query parameter

    CSV format::

        video_subject,video_source,video_aspect,voice_name,video_language,paragraph_number
        高效工作方法,pexels,9:16,,zh,3
        夏日护肤技巧,pixabay,16:9,,zh,5

    JSON format::

        [{"video_subject":"主题1"}, {"video_subject":"主题2","voice_name":"云霞-男声"}]
    """
    from app.services.batch import parse_csv, rows_to_requests
    import json as _json

    # Parse input
    rows_raw = []

    if csv_file is not None and csv_file.filename:
        csv_bytes = await csv_file.read()
        csv_text = csv_bytes.decode("utf-8-sig")
        try:
            rows_raw = parse_csv(csv_text)
        except Exception as _e:
            raise HttpException(
                task_id="batch", status_code=400,
                message=f"Failed to parse CSV: {_e}",
            )
    elif json_body is not None:
        try:
            json_data = _json.loads(json_body)
            if not isinstance(json_data, list):
                raise ValueError("JSON body must be an array")
            rows_raw = json_data
        except Exception as _e:
            raise HttpException(
                task_id="batch", status_code=400,
                message=f"Failed to parse JSON body: {_e}",
            )
    else:
        # Try parsing raw JSON body
        try:
            body_bytes = await request.body()
            if body_bytes:
                body_text = body_bytes.decode("utf-8-sig").strip()
                if body_text.startswith("["):
                    json_data = _json.loads(body_text)
                    if isinstance(json_data, list):
                        rows_raw = json_data
        except Exception:
            pass

    if not rows_raw:
        raise HttpException(
            task_id="batch", status_code=400,
            message="No tasks to create (empty input)",
        )

    # Validate all rows first (fail-fast on schema errors)
    parsed = rows_to_requests(rows_raw)
    errors = [err for _, _, err in parsed if err]
    if errors and len(errors) == len(parsed):
        # All rows failed validation
        raise HttpException(
            task_id="batch", status_code=400,
            message=f"All {len(errors)} row(s) failed validation. First error: {errors[0]}",
        )

    # Submit tasks
    results: list[dict] = []
    for i, (raw_row, task_req, err) in enumerate(parsed):
        if err:
            results.append({"row": i + 1, "status": "error", "error": err, "task_id": None})
            continue

        task_id = utils.get_uuid()
        request_id = base.get_task_id(request)
        try:
            task_data = {
                "task_id": task_id,
                "request_id": request_id,
                "params": task_req.model_dump(),
                "user_id": str(current_user.id),
            }
            sm.state.update_task(task_id, user_id=task_data["user_id"], params=task_data["params"])
            task_manager.add_task(tm.start, task_id=task_id, params=task_req, stop_at=stop_at)
            logger.success(f"Batch task created: {task_id} | subject: {raw_row.get('video_subject', '')[:50]}")
            results.append({
                "row": i + 1,
                "status": "created",
                "task_id": task_id,
                "subject": raw_row.get("video_subject", "")[:100],
            })
        except Exception as _e:
            logger.error(f"Batch task creation failed for row {i + 1}: {_e}")
            results.append({
                "row": i + 1,
                "status": "error",
                "error": str(_e),
                "task_id": None,
            })

    created = sum(1 for r in results if r["status"] == "created")
    failed = sum(1 for r in results if r["status"] == "error")

    return utils.get_response(200, {
        "batch_total": len(results),
        "created": created,
        "failed": failed,
        "tasks": results,
    })
