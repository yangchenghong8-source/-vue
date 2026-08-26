"""只读 / 辅助接口。

这些接口为 Vue 前端提供 WebUI 依赖、但原本只存在于进程内部函数的能力：
声音列表、字体列表、LLM Provider 注册表、配置读写、任务暂停/恢复、缓存统计
与清理、知识库列表、LLM 连通性测试、自定义配音上传。

约定：
- 写入口仅限 ``/config``（写）、``/llm/test``（写）与 ``/custom-audio``（上传）；
- 涉及全局副作用（清理缓存、测试连通性）的接口仅对 admin 开放；
- 所有接口都要求登录（复用 ``app.auth.deps._get_current_user``）。
"""

import os
import pathlib
import tempfile
from uuid import uuid4

from fastapi import Depends, File, Query, Request, UploadFile
from loguru import logger

from app.auth.deps import _get_current_user
from app.auth.models import User
from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models import llm_provider
from app.models.exception import HttpException
from app.services import bgm as bgm_service
from app.services import cache_manager
from app.services import task as tm
from app.services import voice as voice_service
from app.services import state as sm
from app.services.kb_client import kb_client
from app.services.llm import test_connection
from app.utils import utils

router = new_router()

_MASK = "***"


# ── 工具 ────────────────────────────────────────────────────────────────


def _require_admin(user: User) -> None:
    if (user.role or "").lower() != "admin":
        raise HttpException(
            task_id="helper",
            status_code=403,
            message="仅管理员可执行此操作",
        )


def _is_secret_key(key: str) -> bool:
    """判定配置字段是否属于密钥。

    覆盖 ``*_api_key`` / ``*_speech_key`` 等以 ``_key`` 结尾的字段、``*_api_keys``
    （复数）、以及 ``webui_password`` / ``*_secret`` / ``*_token`` 等语义明确的
    密钥后缀，避免把明文凭证返回给前端。
    """
    return (key or "").lower().endswith(
        ("_key", "_keys", "_password", "_secret", "_token", "_passwd")
    )


def _mask_secrets(section: dict) -> dict:
    """返回副本；密钥字段以占位符遮蔽，避免把真实凭证返回给前端。"""
    masked = {}
    for key, value in section.items():
        if _is_secret_key(key):
            masked[key] = _MASK if (value not in ("", None)) else ""
            continue
        if isinstance(value, (dict, list, tuple, set)):
            # 配置里不存在嵌套结构，稳妥起见原样返回
            masked[key] = value
            continue
        masked[key] = value
    return masked


def _apply_section(target, incoming: dict) -> None:
    """把一次 PUT 的字段合并进配置 section。

    密钥字段收到占位符或空串时视为「不修改」，避免把遮蔽后的 ``***`` 回写
    覆盖真实密钥；非密钥字段收到空串/布尔 False 则视为用户主动清空。
    """
    for key, value in incoming.items():
        if _is_secret_key(key):
            if value in ("", None, _MASK):
                continue
        target[key] = value


# ── 声音 / 字体 / 注册表（只读）──────────────────────────────────────────


@router.get("/voices", summary="List all available TTS voices")
def list_voices(request: Request, user: User = Depends(_get_current_user)):
    """聚合各 TTS Provider 的音色，供前端下拉框展示。

    ElevenLabs 依赖已配置的 api_key 在线拉取；未配置或失败时返回空列表。
    """
    voices = {
        "azure": voice_service.get_all_azure_voices(),
        "siliconflow": voice_service.get_siliconflow_voices(),
        "gemini": voice_service.get_gemini_voices(),
        "mimo": voice_service.get_mimo_voices(),
        "elevenlabs": voice_service.get_elevenlabs_voices(
            config.elevenlabs.get("api_key", "")
        ),
        "chatterbox": voice_service.get_chatterbox_voices(),
    }
    return utils.get_response(200, voices)


@router.get("/fonts", summary="List available subtitle fonts")
def list_fonts(request: Request, user: User = Depends(_get_current_user)):
    """枚举 ``resource/fonts`` 下的 .ttf/.ttc 字体文件。"""
    font_dir = utils.font_dir()
    fonts = []
    for root, _dirs, files in os.walk(font_dir):
        for name in files:
            if name.lower().endswith((".ttf", ".ttc")):
                fonts.append(name)
    fonts.sort()
    return utils.get_response(200, {"fonts": fonts})


@router.get("/llm/providers", summary="List LLM provider registry")
def list_llm_providers(request: Request, user: User = Depends(_get_current_user)):
    """返回 Provider 注册表元数据（不含密钥），供设置页动态渲染表单。"""
    providers = []
    for spec in llm_provider.LLM_PROVIDER_REGISTRY:
        providers.append(
            {
                "id": spec.provider_id,
                "label": spec.default_label,
                "default_model": spec.default_model,
                "default_base_url": spec.default_base_url,
                "requires_api_key": spec.requires_api_key,
                "requires_model_name": spec.requires_model_name,
                "requires_base_url": spec.requires_base_url,
                "show_api_key": spec.show_api_key,
                "show_base_url": spec.show_base_url,
                "api_key_url": spec.api_key_url,
            }
        )
    return utils.get_response(
        200,
        {
            "current": str(
                config.app.get(
                    "llm_provider", llm_provider.DEFAULT_LLM_PROVIDER_ID
                )
            ).lower(),
            "providers": providers,
        },
    )


# ── 配置读写 ─────────────────────────────────────────────────────────────


@router.get("/config", summary="Read current configuration (secrets masked)")
def get_config(request: Request, user: User = Depends(_get_current_user)):
    """返回全部配置段；密钥字段一律以 ``***`` 遮蔽。"""
    return utils.get_response(
        200,
        {
            "app": _mask_secrets(dict(config.app)),
            "azure": _mask_secrets(dict(config.azure)),
            "siliconflow": _mask_secrets(dict(config.siliconflow)),
            "elevenlabs": _mask_secrets(dict(config.elevenlabs)),
            "chatterbox": _mask_secrets(dict(config.chatterbox)),
            "ui": _mask_secrets(dict(config.ui)),
        },
    )


@router.put("/config", summary="Update configuration")
def update_config(
    request: Request,
    body: dict,
    user: User = Depends(_get_current_user),
):
    """合并写入配置并原子落盘。仅 admin 可写。"""
    _require_admin(user)

    allowed_sections = ("app", "azure", "siliconflow", "elevenlabs", "chatterbox", "ui")
    targets = {
        "app": config.app,
        "azure": config.azure,
        "siliconflow": config.siliconflow,
        "elevenlabs": config.elevenlabs,
        "chatterbox": config.chatterbox,
        "ui": config.ui,
    }

    applied = 0
    for section_name, incoming in body.items():
        if section_name not in allowed_sections:
            continue
        if not isinstance(incoming, dict):
            continue
        _apply_section(targets[section_name], incoming)
        applied += 1

    if applied:
        config.save_config()

    return utils.get_response(200, {"saved": applied > 0})


# ── 任务暂停 / 恢复 ──────────────────────────────────────────────────────


def _owned_task(task_id: str, user: User) -> dict:
    """返回属于当前用户（或 admin 可见的 legacy）的任务，否则抛 404。"""
    task = sm.state.get_task(task_id)
    if not task:
        raise HttpException(
            task_id=task_id, status_code=404, message=f"{task_id}: task not found"
        )
    owner = task.get("user_id")
    if owner is None:
        if (user.role or "").lower() != "admin":
            raise HttpException(
                task_id=task_id, status_code=404, message=f"{task_id}: task not found"
            )
    elif str(owner) != str(user.id):
        raise HttpException(
            task_id=task_id, status_code=404, message=f"{task_id}: task not found"
        )
    return task


@router.post("/tasks/{task_id}/pause", summary="Pause a running task")
def pause_task(
    task_id: str,
    request: Request,
    user: User = Depends(_get_current_user),
):
    _owned_task(task_id, user)
    if not tm.is_task_busy(sm.state.get_task(task_id)):
        raise HttpException(
            task_id=task_id, status_code=409, message=f"{task_id}: task is not running"
        )
    tm.pause_task(task_id)
    return utils.get_response(200, {"task_id": task_id, "paused": True})


@router.post("/tasks/{task_id}/resume", summary="Resume a paused task")
def resume_task(
    task_id: str,
    request: Request,
    user: User = Depends(_get_current_user),
):
    _owned_task(task_id, user)
    tm.resume_task(task_id)
    return utils.get_response(200, {"task_id": task_id, "resumed": True})


# ── 缓存管理（admin）─────────────────────────────────────────────────────


@router.get("/cache", summary="Get video material cache statistics")
def get_cache_stats(
    request: Request,
    max_age_days: int | None = Query(None, description="Preview candidates older than N days"),
    user: User = Depends(_get_current_user),
):
    _require_admin(user)
    stats = cache_manager.get_video_cache_stats(max_age_days)
    return utils.get_response(
        200,
        {
            "file_count": stats.file_count,
            "total_size": stats.total_size,
            "oldest_mtime": stats.oldest_mtime,
            "newest_mtime": stats.newest_mtime,
            "dir": cache_manager.video_cache_dir(),
        },
    )


@router.delete("/cache", summary="Clean video material cache")
def clean_cache(
    request: Request,
    max_age_days: int | None = Query(None, description="Only remove files older than N days"),
    user: User = Depends(_get_current_user),
):
    _require_admin(user)
    try:
        result = cache_manager.clean_video_cache(max_age_days)
    except ValueError as exc:
        raise HttpException(
            task_id="cache", status_code=400, message=f"cache: {str(exc)}"
        )
    return utils.get_response(
        200,
        {
            "deleted_count": result.deleted_count,
            "deleted_size": result.deleted_size,
            "failed_count": result.failed_count,
        },
    )


# ── 知识库（只读）────────────────────────────────────────────────────────


@router.get("/kb/health", summary="Check knowledge base availability")
def kb_health(request: Request, user: User = Depends(_get_current_user)):
    return utils.get_response(200, {"healthy": kb_client.is_healthy()})


@router.get("/kb/documents", summary="List knowledge base documents")
def kb_documents(
    request: Request,
    search: str = Query("", description="Filter by filename keyword"),
    category: str = Query("", description="Filter by category"),
    user: User = Depends(_get_current_user),
):
    return utils.get_response(
        200, {"documents": kb_client.list_documents(search=search, category=category)}
    )


@router.get("/kb/categories", summary="List knowledge base categories")
def kb_categories(request: Request, user: User = Depends(_get_current_user)):
    return utils.get_response(200, {"categories": kb_client.list_categories()})


@router.get("/kb/media/categories", summary="List knowledge base media category tree")
def kb_media_categories(
    request: Request,
    file_type: str = Query("all", description="all / image / video / doc"),
    user: User = Depends(_get_current_user),
):
    return utils.get_response(
        200, {"tree": kb_client.list_media_category_tree(file_type)}
    )


# ── LLM 连通性测试（admin）───────────────────────────────────────────────


@router.post("/llm/test", summary="Test LLM provider connectivity")
def llm_test_connection(
    request: Request,
    user: User = Depends(_get_current_user),
):
    _require_admin(user)
    ok, message, elapsed = test_connection()
    return utils.get_response(
        200, {"success": ok, "message": message, "elapsed": round(elapsed, 3)}
    )


# ── 自定义配音上传 ────────────────────────────────────────────────────────
#
# 供 Vue 前端在「上传配音」模式下使用。复用 BGM 服务的文件名/音频流校验，
# 但存到独立的 storage/uploaded_audio 目录，并返回项目相对路径，这样
# createVideo 的 custom_audio_file 字段能被 resolve_custom_audio_file 解析。

_MAX_CUSTOM_AUDIO_BYTES = 50 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


def _remove_upload_file(file_path: str) -> None:
    """尽力清理上传临时/残留文件，且不覆盖调用方正在处理的原始异常。"""
    if not file_path or not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
    except OSError as exc:
        logger.warning(
            f"failed to remove custom audio file: path={file_path}, error={str(exc)}"
        )


_CUSTOM_AUDIO_UPLOAD_PREFIX = ".custom-audio-"


def _voiceover_error_message(exc: bgm_service.BgmUploadError) -> str:
    """把 BGM 服务的错误文案翻译成 custom-audio 语境，避免对配音上传误报“背景音乐”。"""
    return str(exc).replace("background music", "custom audio")


@router.post("/custom-audio", summary="Upload a custom voiceover audio file")
def upload_custom_audio(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(_get_current_user),
):
    request_id = base.get_task_id(request)

    try:
        safe_name = bgm_service.sanitize_upload_filename(file.filename)
    except bgm_service.BgmUploadError as exc:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {_voiceover_error_message(exc)}",
        )

    stored_name = f"{uuid4().hex}{pathlib.Path(safe_name).suffix.lower()}"
    target_dir = utils.storage_dir("uploaded_audio", create=True)
    target_path = os.path.join(target_dir, stored_name)
    temp_path = ""

    try:
        # 先写同目录临时文件、校验通过后再原子替换，避免进程中断或校验失败在
        # storage/uploaded_audio 留下半个音频文件（复用 BGM 的 staging 模式）。
        descriptor, temp_path = tempfile.mkstemp(
            prefix=_CUSTOM_AUDIO_UPLOAD_PREFIX,
            suffix=pathlib.Path(safe_name).suffix.lower(),
            dir=target_dir,
        )
        with os.fdopen(descriptor, "wb") as buffer:
            file.file.seek(0)
            total_bytes = 0
            while True:
                chunk = file.file.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise bgm_service.BgmUploadError(
                        "custom audio upload must be binary"
                    )
                total_bytes += len(chunk)
                if total_bytes > _MAX_CUSTOM_AUDIO_BYTES:
                    raise bgm_service.BgmUploadError(
                        "custom audio file exceeds the 50 MB limit"
                    )
                buffer.write(chunk)
            buffer.flush()
            os.fsync(buffer.fileno())

        if total_bytes == 0:
            raise bgm_service.BgmUploadError("custom audio file is empty")

        # 复用 BGM 的 FFmpeg 音频流校验（公开接口，超时 120s）
        bgm_service.validate_audio_file(temp_path)
        os.replace(temp_path, target_path)
        temp_path = ""
    except bgm_service.BgmUploadError as exc:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {_voiceover_error_message(exc)}",
        )
    except bgm_service.BgmServiceError:
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: custom audio validation is unavailable",
        )
    except OSError:
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: failed to store custom audio",
        )
    except Exception as exc:
        logger.error(
            f"unexpected custom audio upload failure: request_id={request_id}, "
            f"error_type={type(exc).__name__}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: failed to store custom audio",
        ) from exc
    finally:
        # 无论成功还是任何异常路径，都清理可能残留的临时文件（成功时已置空）。
        _remove_upload_file(temp_path)

    # 返回项目相对路径（含 storage/uploaded_audio 前缀），
    # resolve_custom_audio_file 会以 root_dir 为基准解析并校验路径不越界。
    relative_path = os.path.join("storage", "uploaded_audio", stored_name)
    return utils.get_response(200, {"file": relative_path})
