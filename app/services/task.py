import math
import os
import re
import signal
import socket
import threading
import time
import jieba
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from functools import partial
from os import path
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import bgm as bgm_service
from app.services import (
    elevenlabs_music,
    llm,
    material,
    sonilo,
    subtitle,
    twelvelabs,
    video,
    voice,
)
from app.services import upload_post
from app.services import state as sm
from app.services.kb_client import kb_client
from app.services.vision import vision_client
from app.services.jimeng import jimeng_client
from app.services.logging_setup import set_trace_id as _set_trace_id
from app.services import checkpoint as cp
from app.utils import file_security, utils


# 发布请求最长可等待数分钟，不能继续占用视频生成任务的并发名额。
# 固定大小的线程池将发布吞吐限制在可控范围内，同时让视频产物生成后
# 立即进入完成状态。
_cross_post_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="mpt-cross-post",
)
_cross_post_max_pending_tasks = max(
    1,
    int(config.app.get("upload_post_max_pending_tasks", 10)),
)
_cross_post_slots = threading.BoundedSemaphore(_cross_post_max_pending_tasks)
_cross_post_registry_lock = threading.RLock()
_cross_post_futures: dict[str, Future] = {}
_cross_post_process_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
_ACTIVE_CROSS_POST_STATES = {
    const.CROSS_POST_STATE_PENDING,
    const.CROSS_POST_STATE_PROCESSING,
}
_CROSS_POST_STATE_WRITE_ATTEMPTS = 3
_CROSS_POST_STATE_RETRY_DELAY_SECONDS = 0.1
_INTERRUPTED_CROSS_POST_ERROR = (
    "cross-posting was interrupted before the process completed"
)
# 视频配乐服务只需实现 ``is_enabled`` 和 ``generate_bgm``。供应商差异集中在
# 文件扩展名、领域异常和 WebUI 警告代码；任务编排、0 音量短路及失败降级
# 全部复用同一路径，避免后续新增供应商时维护多份相似流程。
_VIDEO_MUSIC_PROVIDERS = {
    "sonilo": {
        "service": sonilo,
        "error_type": sonilo.SoniloError,
        "suffix": ".m4a",
        "warning_code": "sonilo_bgm_failed",
        "display_name": "Sonilo",
    },
    "elevenlabs": {
        "service": elevenlabs_music,
        "error_type": elevenlabs_music.ElevenLabsMusicError,
        "suffix": ".mp3",
        "warning_code": "elevenlabs_bgm_failed",
        "display_name": "ElevenLabs",
    },
}



def _get_video_music_prompt(params: VideoParams) -> str:
    """
    读取当前视频配乐供应商实际使用的提示词。

    新任务统一使用供应商无关字段；旧 Sonilo CLI 参数和历史任务仍可能只有
    ``sonilo_bgm_prompt``，因此仅在 Sonilo 通用字段为空时读取旧字段。
    """
    prompt = str(params.video_music_prompt or "").strip()
    if params.bgm_type == "sonilo" and not prompt:
        prompt = str(params.sonilo_bgm_prompt or "").strip()
    return prompt


# 视频生成暂停控制：流水线在阶段边界检查暂停标志。Event.set() 表示请求暂停，
# Event.clear() 表示恢复运行。进程级字典跨 WebUI 会话共享，任务完成后由
# WebUI 提交新任务或进程退出时自然回收；Event 本身占内存极小，无需主动清理。
_pause_events: dict[str, threading.Event] = {}
_pause_events_lock = threading.RLock()


def _get_pause_event(task_id: str) -> threading.Event:
    with _pause_events_lock:
        return _pause_events.setdefault(task_id, threading.Event())


def _find_ffmpeg_pids() -> list[int]:
    """找出当前进程所有 ffmpeg 后代进程 PID，用于实时暂停视频编码。

    容器内没有 ps 命令，直接遍历 /proc 构建进程树，按 cmdline 匹配 ffmpeg。
    WebUI 生成任务是单并发，这些 ffmpeg 进程都属于当前任务，不会误伤其它任务。
    """
    me = os.getpid()
    children: dict[int, list[int]] = {}
    cmdlines: dict[int, str] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
                stat = f.read()
            rparen = stat.rfind(")")
            ppid = int(stat[rparen + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdlines[pid] = f.read().replace(bytes([0]), b" ").decode(
                    errors="ignore"
                )
        except OSError:
            cmdlines[pid] = ""
        children.setdefault(ppid, []).append(pid)

    ffmpeg_pids: list[int] = []
    stack = [me]
    while stack:
        parent = stack.pop()
        for child in children.get(parent, []):
            if "ffmpeg" in cmdlines.get(child, ""):
                ffmpeg_pids.append(child)
            stack.append(child)
    return ffmpeg_pids


def _signal_ffmpeg(sig: int) -> None:
    """对所有 ffmpeg 后代进程发送信号（SIGSTOP 暂停 / SIGCONT 恢复）。"""
    for pid in _find_ffmpeg_pids():
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def pause_task(task_id: str) -> bool:
    """请求暂停任务：置位暂停标志并持久化暂停状态。"""
    _get_pause_event(task_id).set()
    sm.state.patch_task(task_id, state=const.TASK_STATE_PAUSED)
    _signal_ffmpeg(signal.SIGSTOP)
    logger.info(f"task pause requested: task_id={task_id}")
    return True


def resume_task(task_id: str) -> bool:
    """恢复暂停的任务：清除暂停标志并恢复处理中状态。"""
    _signal_ffmpeg(signal.SIGCONT)
    _get_pause_event(task_id).clear()
    sm.state.patch_task(task_id, state=const.TASK_STATE_PROCESSING)
    logger.info(f"task resumed: task_id={task_id}")
    return True


def is_task_paused(task_id: str) -> bool:
    return _get_pause_event(task_id).is_set()


def _wait_if_paused(task_id: str) -> None:
    """阶段边界检查：任务被请求暂停时阻塞，直到恢复运行。

    暂停粒度是「阶段间」：当前正在执行的步骤（LLM 生成、TTS、素材下载或
    FFmpeg 合成）会先跑完，进入下一阶段前停住。这样不会中断第三方 API 调用
    或 FFmpeg 子进程，也不会浪费已经消耗的资源。
    """
    event = _get_pause_event(task_id)
    if not event.is_set():
        return
    # pause_task 已写入暂停状态；这里再确认一次，覆盖直接调用本函数的场景，
    # 保证轮询方能看到一致的 PAUSED 状态。
    sm.state.patch_task(task_id, state=const.TASK_STATE_PAUSED)
    logger.info(f"task paused at stage boundary: task_id={task_id}")
    while event.is_set():
        event.wait(timeout=1.0)
    sm.state.patch_task(task_id, state=const.TASK_STATE_PROCESSING)
    logger.info(f"task resumed execution: task_id={task_id}")


def is_task_busy(task: dict | None) -> bool:
    """判断任务是否仍在生成或发布，供所有删除入口复用。"""
    if not task:
        return False

    state = task.get("state")
    try:
        state = int(state)
    except (TypeError, ValueError):
        pass

    # 视频生成和跨平台发布都可能继续读取任务目录。统一视为忙碌状态，
    # 可以避免 API 与 WebUI 分别维护规则后出现一个允许删除、另一个禁止
    # 删除的不一致行为。暂停中的任务仍会继续执行，同样不可删除。
    return (
        state in (const.TASK_STATE_PROCESSING, const.TASK_STATE_PAUSED)
        or task.get("cross_post_state") in _ACTIVE_CROSS_POST_STATES
    )


def _register_cross_post_future(task_id: str, future: Future) -> None:
    """登记当前进程持有的发布 Future，供启动恢复和测试判断真实运行状态。"""
    with _cross_post_registry_lock:
        _cross_post_futures[task_id] = future


def _unregister_cross_post_future(task_id: str, future: Future | None = None) -> None:
    """仅移除匹配的 Future，避免旧回调误删同任务后续注册的新工作。"""
    with _cross_post_registry_lock:
        current = _cross_post_futures.get(task_id)
        if current is None or (future is not None and current is not future):
            return
        _cross_post_futures.pop(task_id, None)


def _is_cross_post_active_in_process(task_id: str) -> bool:
    """判断当前进程是否仍持有未结束的发布任务。"""
    with _cross_post_registry_lock:
        future = _cross_post_futures.get(task_id)
        return future is not None and not future.done()


def _is_windows_process_alive(process_id: int) -> bool:
    """通过只读 Win32 API 判断进程状态，避免用 os.kill 误终止进程。"""
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # ctypes 默认把未声明的返回值当作 32 位 int。Windows 64 位进程句柄可能
    # 因此被截断，必须显式声明 Win32 函数签名后再调用。
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code == error_invalid_parameter:
            return False
        if error_code == error_access_denied:
            # 进程存在但当前用户无查询权限时，必须保守地视为存活，避免错误
            # 回收其它账户正在执行的发布任务。
            return True
        logger.warning(
            "failed to open cross-post owner process on Windows, "
            f"process_id: {process_id}, error_code: {error_code}"
        )
        return True

    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            error_code = ctypes.get_last_error()
            logger.warning(
                "failed to read cross-post owner process state on Windows, "
                f"process_id: {process_id}, error_code: {error_code}"
            )
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _is_cross_post_owner_alive(owner: str | None) -> bool:
    """判断持久化发布任务的本机进程是否仍存在。"""
    if not owner:
        return False

    try:
        hostname, process_id_text, _ = owner.split(":", 2)
        process_id = int(process_id_text)
    except (TypeError, ValueError):
        logger.warning(f"invalid cross-post owner metadata: {owner}")
        return False

    # 无法可靠探测其它主机上的进程。共享 Redis 的多主机部署中必须保守地
    # 视为仍在运行，避免当前节点误删另一节点正在读取的视频文件。
    if hostname != socket.gethostname():
        return True

    # 当前进程内是否仍有真实发布工作，已经由 Future 注册表准确判断。运行到
    # 这里说明注册表中没有对应 Future，即使 owner 与当前进程完全一致，也应
    # 视为已中断；这可以覆盖终态写入持续失败、Future 已结束的场景。
    if process_id == os.getpid():
        return False

    # Windows 的 os.kill(pid, 0) 与 POSIX 语义不同，可能直接终止目标进程。
    # 使用只申请查询权限的 Win32 API，不向目标进程发送任何信号。
    if os.name == "nt":
        return _is_windows_process_alive(process_id)

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        logger.warning(
            f"failed to inspect cross-post owner process, owner: {owner}, error: {exc}"
        )
        return True
    return True


def _mark_task_failed(task_id: str, stage: str, error: str) -> dict:
    """记录结构化失败信息，并保留任务失败前已经到达的进度。"""
    existing_task = None
    try:
        existing_task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.warning(f"failed to read task state before failure update: {exc}")

    # 具体服务函数通常比编排层拥有更准确的错误原因。后续的空结果检查
    # 不能再用通用文案覆盖它，否则 API 调用方仍然只能看到模糊信息。
    if (
        existing_task
        and existing_task.get("state") == const.TASK_STATE_FAILED
        and existing_task.get("error")
    ):
        return existing_task

    message = str(error or "unknown task error").strip()
    progress = int((existing_task or {}).get("progress", 0) or 0)
    logger.error(
        f"task failed, task_id: {task_id}, stage: {stage}, error: {message}"
    )
    failure = {
        "task_id": task_id,
        "state": const.TASK_STATE_FAILED,
        "progress": progress,
        "failed_stage": stage,
        "error": message,
    }
    sm.state.update_task(
        task_id,
        state=failure["state"],
        progress=failure["progress"],
        failed_stage=failure["failed_stage"],
        error=failure["error"],
    )
    cp.mark_failed(task_id, stage, message, failure["progress"])
    return failure


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    kb_info = {"used": False, "fallback": False, "chunks": 0, "empty": False}
    if not video_script:
        use_kb = getattr(params, "use_knowledge", False)
        kb_docs = getattr(params, "kb_doc_filenames", None)
        video_script, kb_info = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            video_script_prompt=params.video_script_prompt,
            custom_system_prompt=params.custom_system_prompt,
            use_knowledge=use_kb,
            kb_doc_filenames=kb_docs,
            target_duration=getattr(params, "video_script_duration", 0) or 0,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        _mark_task_failed(task_id, "script", "failed to generate video script")
        return None, kb_info

    return video_script, kb_info


def _build_kb_media_from_category(category) -> list:
    """按选中的知识库分类（一级目录）取素材清单（含 description，已抽样）。

    用于素材驱动模式：分类是唯一素材来源。抽样在 kb_client.list_media_sampled
    （优先有视觉描述的素材，截断到上限，多余素材被裁剪）。
    失败/空返回 []，调用方回退 legacy 流程。
    """
    cat = (category or "").strip()
    if not cat:
        return []
    try:
        media = kb_client.list_media_sampled(cat)
    except Exception as _e:
        logger.warning(f"material-driven: list_media_sampled({cat}) failed: {_e}")
        return []
    return media


def _parse_paragraph_durations(subtitle_path: str, video_script: str) -> list[float]:
    """Parse SRT to extract per-paragraph spoken durations for shot timing.

    Splits the script into paragraphs, then matches each paragraph to its
    subtitle entries to determine start/end times. Returns a list of
    durations (seconds), one per paragraph, in script order.
    """
    if not subtitle_path or not os.path.exists(subtitle_path):
        logger.warning("SRT not found, cannot compute paragraph durations")
        return []

    # Parse SRT entries
    srt_entries = []
    with open(subtitle_path, "r", encoding="utf-8") as _f:
        _text = _f.read()
    _blocks = _text.strip().split("\n\n")
    for _b in _blocks:
        _lines = _b.strip().split("\n")
        if len(_lines) < 2:
            continue
        _time_line = _lines[1]
        _text_line = " ".join(_lines[2:]).strip()
        _match = re.match(
            r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})",
            _time_line,
        )
        if _match:
            _start = _ts_to_seconds(_match.group(1))
            _end = _ts_to_seconds(_match.group(2))
            srt_entries.append((_text_line, _start, _end))

    if not srt_entries:
        return []

    # Split script into paragraphs
    paragraphs = [p.strip() for p in video_script.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in video_script.split("\n") if p.strip()]
    if len(paragraphs) <= 1:
        # Single-paragraph script: return empty; pipeline will use keyword-count fallback.
        return []

    # Normalize text for matching
    def _clean(t: str) -> str:
        # Remove punctuation and spaces for fuzzy matching
        return re.sub(r"[，,。、；;：:！!？?\s\u3000]", "", t)

    _srt_texts = [_clean(e[0]) for e in srt_entries]

    durations = []
    _entry_idx = 0
    for _para in paragraphs:
        _para_clean = _clean(_para)
        if not _para_clean:
            continue
        _para_start = None
        _para_end = None
        # Find first and last SRT entry that appears in this paragraph
        while _entry_idx < len(srt_entries):
            _entry_text, _entry_start, _entry_end = srt_entries[_entry_idx]
            if _clean(_entry_text) in _para_clean:
                if _para_start is None:
                    _para_start = _entry_start
                _para_end = _entry_end
                _entry_idx += 1
            else:
                if _para_start is not None:
                    # Started this paragraph, now hit next paragraph's text
                    break
                else:
                    _entry_idx += 1
        if _para_start is not None and _para_end is not None:
            _dur = max(0.5, _para_end - _para_start)
            durations.append(_dur)
            logger.info(
                f"paragraph \"{_para[:30]}...\" -> {_dur:.2f}s "
                f"({_para_start:.2f} - {_para_end:.2f})"
            )
        else:
            # Paragraph not found in SRT — estimate based on text length
            _est = max(1.0, len(_para) / 6.0)  # rough: ~6 chars/sec for Chinese
            durations.append(_est)
            logger.warning(
                f"paragraph \"{_para[:30]}...\" not found in SRT, "
                f"estimated duration: {_est:.2f}s"
            )

    if durations:
        logger.info(f"paragraph durations: {durations}, total: {sum(durations):.2f}s")
    return durations


def _ts_to_seconds(ts: str) -> float:
    """Convert SRT timestamp like '00:01:23,456' to seconds."""
    ts = ts.replace(",", ".")
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    if not video_terms:
        # 开启素材按文案顺序匹配后，关键词本身也必须按脚本叙事顺序生成；
        # 否则后续即使顺序下载和顺序拼接，也只能复用一组全局主题词，
        # 无法改善“后面内容的画面提前出现”的问题。
        # 分镜模式：每个段落配一个关键词，一一对应
        _para_count = len([p for p in video_script.split("\\n\\n") if p.strip()])
        if _para_count <= 1:
            _para_count = len([p for p in video_script.split("\\n") if p.strip()])
        _terms_amount = max(_para_count, 5) if params.match_materials_to_script else 5
        video_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=video_script,
            amount=_terms_amount,
            match_script_order=params.match_materials_to_script,
            source=getattr(params, 'video_source', 'pexels') or 'pexels',
        )
        logger.info(
            f"generating {_terms_amount} terms for {_para_count} paragraphs "
            f"(match_script_order={params.match_materials_to_script})"
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        _mark_task_failed(
            task_id,
            "terms",
            "failed to generate video search terms",
        )
        return None

    # 可选的 TwelveLabs Marengo 语义重排：未启用时返回原顺序，无任何副作用。
    # 顺序匹配模式下关键词顺序本身就是脚本叙事顺序，必须保持原样，故跳过。
    if not params.match_materials_to_script:
        video_terms = twelvelabs.rerank_terms_by_subject(
            video_subject=params.video_subject,
            search_terms=video_terms,
        )

    return video_terms


def save_script_data(task_id, video_script, video_terms, params, storyboard=None, topic_terms=None):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }
    if storyboard:
        script_data["storyboard"] = storyboard
    if topic_terms:
        script_data["topic_terms"] = topic_terms
    if params and getattr(params, "kb_category", ""):
        script_data["kb_category"] = params.kb_category

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def resolve_custom_audio_file(task_id: str, custom_audio_file: str | None) -> str:
    requested_file = (custom_audio_file or "").strip()
    if not requested_file:
        return ""

    task_dir = utils.task_dir(task_id)
    try:
        return file_security.resolve_path_within_directory(
            task_dir,
            requested_file,
        )
    except ValueError as exc:
        task_dir_error = exc

    server_audio_file = path.realpath(
        requested_file
        if path.isabs(requested_file)
        else path.join(utils.root_dir(), requested_file)
    )
    if not path.isabs(requested_file):
        project_root = path.realpath(utils.root_dir())
        try:
            if path.commonpath([project_root, server_audio_file]) != project_root:
                raise ValueError(
                    "relative custom audio paths must stay within the project directory"
                )
        except ValueError as exc:
            raise ValueError(
                "custom audio file must be task-local or an existing server-side file"
            ) from exc

    if not path.isfile(server_audio_file):
        raise ValueError(
            "custom audio file does not exist or is not a file"
        ) from task_dir_error

    return server_audio_file


def _resolve_reusable_voice_preview(
    task_id: str,
    params,
    video_script: str,
    voice_preview: dict | None,
) -> tuple[str, float, object] | None:
    """
    校验并解析 WebUI 提交的完整试听缓存。

    该载荷不是公开 API 参数，只能来自当前进程的 WebUI。即便如此，后台任务
    仍重新核对文案和全部配音参数，并限制音频位于当前任务目录；任何不一致都
    回退普通 TTS，不让过期试听污染正式成片。
    """
    if not voice_preview:
        return None

    expected_values = {
        "script": str(video_script or "").strip(),
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }
    if not math.isclose(float(params.voice_volume), 1.0) or any(
        voice_preview.get(key) != value for key, value in expected_values.items()
    ):
        logger.info(
            f"skip stale voice preview cache, task_id: {task_id}, "
            "reason: voice parameters changed"
        )
        return None

    preview_file = path.realpath(str(voice_preview.get("audio_file") or ""))
    task_root = path.realpath(utils.task_dir(task_id))
    try:
        preview_is_task_local = path.commonpath([task_root, preview_file]) == task_root
    except ValueError:
        preview_is_task_local = False

    duration = voice_preview.get("duration")
    sub_maker = voice_preview.get("sub_maker")
    if (
        not preview_is_task_local
        or not path.isfile(preview_file)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
        or sub_maker is None
    ):
        logger.warning(
            f"skip invalid voice preview cache, task_id: {task_id}, "
            f"audio_file: {preview_file or '<empty>'}"
        )
        return None

    logger.info(
        f"using full voice preview audio, task_id: {task_id}, duration: {duration:.2f}s"
    )
    return preview_file, math.ceil(duration), sub_maker


def generate_audio(task_id, params, video_script, voice_preview=None, variant_index=0):
    """
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    """
    logger.info("\n\n## generating audio")
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    requested_custom_audio_file = getattr(params, "custom_audio_file", None)
    try:
        custom_audio_file = resolve_custom_audio_file(
            task_id, requested_custom_audio_file
        )
    except ValueError as exc:
        _mark_task_failed(
            task_id,
            "audio",
            f"invalid custom audio file: {exc}",
        )
        return None, None, None

    if not custom_audio_file:
        reusable_preview = _resolve_reusable_voice_preview(
            task_id,
            params,
            video_script,
            voice_preview,
        )
        if reusable_preview:
            return reusable_preview

        logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(
            utils.task_dir(task_id),
            f"audio-{variant_index}.mp3" if variant_index > 0 else "audio.mp3",
        )
        sub_maker = voice.tts(
            text=video_script,
            voice_name=voice.parse_voice_name(params.voice_name),
            voice_rate=params.voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            _mark_task_failed(
                task_id,
                "audio",
                "failed to synthesize audio; verify the selected voice and TTS connectivity",
            )
            return None, None, None
        audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
        if audio_duration == 0:
            _mark_task_failed(task_id, "audio", "generated audio duration is zero")
            return None, None, None
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            _mark_task_failed(
                task_id,
                "audio",
                "custom audio duration is zero",
            )
            return None, None, None
        return custom_audio_file, audio_duration, None

def generate_subtitle(task_id, params, video_script, sub_maker, audio_file, variant_index=0):
    '''
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    '''
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled:
        return ""

    subtitle_path = path.join(
        utils.task_dir(task_id),
        f"subtitle-{variant_index}.srt" if variant_index > 0 else "subtitle.srt",
    )
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    if not subtitle_provider:
        logger.info("subtitle provider is empty, skip subtitle generation")
        return ""

    if sub_maker is None and subtitle_provider != "whisper":
        # 自定义音频不会经过 TTS，因此没有 Edge/Azure 等 TTS 返回的
        # sub_maker 时间轴。只有 Whisper 可以直接从音频文件转写字幕；
        # 其他字幕提供方继续保持原有行为，避免生成错误的空时间轴。
        logger.warning(
            "subtitle maker is missing, skip subtitle generation for provider: "
            f"{subtitle_provider}"
        )
        return ""

    if subtitle_provider == "edge":
        voice.create_subtitle(
            text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
        )
        if not os.path.exists(subtitle_path):
            # Edge 字幕偶尔会因为时间轴与文案无法匹配而没有产出文件。这里不能
            # 自动切换到 Whisper，否则首次失败会在用户不知情的情况下下载数 GB
            # 的模型。只有显式配置 Whisper 时才允许加载模型，Edge 失败则保留
            # 无字幕视频并记录原因，避免意外的网络和磁盘开销。
            logger.warning(
                "edge subtitle generation did not produce a subtitle file; "
                "skip subtitles without falling back to whisper"
            )
            return ""

    if subtitle_provider == "whisper":
        subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
        logger.info("\n\n## correcting subtitle")
        subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def _generate_jimeng_storyboard(task_id, params, kb_category) -> list:
    """即梦模式第一步：知识库取图 → Kimi 识图，产出分镜（含客观视觉描述）。

    返回 shots 列表（每项含 index/media/local_path/visual_description/
    duration/clip_duration）；失败返回空列表。
    """
    from app.services.storyboard import generate_promo_storyboard

    _task_dir = utils.task_dir(task_id)
    storyboard = generate_promo_storyboard(
        kb_category=kb_category,
        task_dir=_task_dir,
        video_subject=getattr(params, "video_subject", "") or "",
    )
    if not storyboard:
        logger.error(f"jimeng mode: storyboard generation failed for '{kb_category}'")
        return []
    return storyboard


def _parse_frontend_jimeng_storyboard(raw: str) -> list:
    """解析前端传来的即梦分镜 JSON（字符串），失败返回空列表。

    兼容 list 与 {shots: [...]} 两种结构，便于后续扩展。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        import json as _json
        data = _json.loads(raw)
    except Exception as _e:
        logger.warning(f"jimeng mode: invalid frontend storyboard JSON: {_e}")
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("shots") or []
    return []


def _reuse_jimeng_storyboard(task_id: str, reused_shots: list) -> list:
    """复用前端编辑过的分镜：按 media 文件名重新下载图，补齐 local_path。"""
    from app.services.storyboard import reuse_promo_storyboard

    _task_dir = utils.task_dir(task_id)
    shots = reuse_promo_storyboard(reused_shots, _task_dir)
    if not shots:
        logger.error("jimeng mode: reuse storyboard produced no downloadable shots")
    return shots


def _generate_jimeng_videos(task_id, params, shots) -> list:
    """即梦模式第二步：基于识图结果 + DeepSeek 画面提示词 → 即梦图生视频片段。

    并行提交所有镜头的图生视频任务：方舟异步任务本身支持并发，串行会因每段
    100~150s 的「提交→轮询→下载」把整体拖到 15 分钟以上；这里用线程池同时
    提交+轮询，总耗时收敛到最慢一段。单段失败不影响其余镜头。
    返回视频路径列表（按镜头 index 顺序）；失败时返回空列表。
    """
    _task_dir = utils.task_dir(task_id)

    # 即梦 ratio 直接复用视频宽高比（"9:16" / "16:9" / "1:1"）
    _ratio = getattr(params, "video_aspect", "9:16")
    if hasattr(_ratio, "value"):
        _ratio = _ratio.value
    _ratio = str(_ratio or "9:16")

    def _gen_one(shot: dict) -> tuple[int, str | None]:
        _out = path.join(_task_dir, f"jimeng-shot-{shot['index']:02d}.mp4")
        try:
            return shot["index"], jimeng_client.image_to_video(
                image_path=shot["local_path"],
                prompt=shot.get("camera_prompt") or shot.get("visual_description", ""),
                output_path=_out,
                duration=int(shot["duration"]),
                ratio=_ratio,
            )
        except Exception as _e:
            # 单个镜头失败不拖垮整条任务；至少保留成功的镜头
            logger.warning(f"jimeng shot {shot['index']} failed: {_e}")
            return shot["index"], None

    # 并发上限取镜头数与 8 的较小值（分镜通常 6~8 镜头）
    max_workers = min(len(shots), 8) or 1
    results: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_gen_one, s) for s in shots]
        for fut in as_completed(futures):
            idx, video_path = fut.result()
            results[idx] = video_path

    videos = [results[s["index"]] for s in shots if results.get(s["index"])]
    if not videos:
        logger.error("jimeng mode: all shots failed to generate")
    return videos


def get_video_materials(task_id, params, video_terms, audio_duration, kb_fallback_to_pexels=False, kb_category=""):
    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials,
            clip_duration=params.video_clip_duration,
            video_aspect=params.video_aspect,
        )
        if not materials:
            _mark_task_failed(
                task_id,
                "materials",
                "no valid local video materials were found",
            )
            return None
        return [material_info.url for material_info in materials]
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        # 顺序匹配模式只在用户显式开启时生效。这里强制素材下载按关键词顺序
        # 轮询，避免某个早期关键词下载太多素材，把后续脚本主题挤出最终时间线。
        downloaded_videos = material.download_videos(
            task_id=task_id,
            search_terms=video_terms,
            source=params.video_source,
            video_aspect=params.video_aspect,
            video_concat_mode=(
                VideoConcatMode.sequential
                if params.match_materials_to_script
                else params.video_concat_mode
            ),
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
            match_script_order=params.match_materials_to_script,
            kb_fallback_to_pexels=kb_fallback_to_pexels,
                kb_category=kb_category,
        )
        if not downloaded_videos:
            # KB 模式下空素材不是致命错误，视频组装层会循环复用
            if params.video_source == "knowledge_base":
                logger.warning(
                    "KB returned no materials; continuing with empty list. "
                    "Video assembly will use placeholders."
                )
                return []
            _mark_task_failed(
                task_id,
                "materials",
                f"failed to download video materials from {params.video_source}",
            )
            return None
        return downloaded_videos


def generate_final_videos(
    task_id, params, downloaded_videos, audio_files, subtitle_paths,
    audio_durations, clip_durations=None,
):
    final_video_paths = []
    combined_video_paths = []
    warnings = []
    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_requested = (
        video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    # 多视频生成默认会打散素材以增加差异；但“按文案顺序匹配素材”追求的是
    # 时间线稳定性和可解释性，所以开启后所有输出都使用顺序拼接。
    if params.match_materials_to_script:
        video_concat_mode = VideoConcatMode.sequential
    elif params.video_count == 1:
        video_concat_mode = params.video_concat_mode
    else:
        video_concat_mode = VideoConcatMode.random
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        audio_file = audio_files[i]
        subtitle_path = subtitle_paths[i]
        audio_duration = audio_durations[i]
        _wait_if_paused(task_id)
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=downloaded_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=params.video_clip_duration,
            threads=params.n_threads,
            clip_speed=params.video_clip_speed,
            clip_durations=clip_durations,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        _wait_if_paused(task_id)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        # 视频配乐模式先明确禁用默认 BGM 解析，避免旧任务残留的 bgm_file 被
        # 误用。只有音量大于 0 才生成代理并调用付费 API；0 音量统一跳过。
        bgm_file_override = "" if video_music_provider else None
        if video_music_requested:
            service = video_music_provider["service"]
            display_name = video_music_provider["display_name"]
            warning_code = video_music_provider["warning_code"]
            generated_bgm_path = path.join(
                utils.task_dir(task_id),
                (f"{params.bgm_type}-bgm-{index}{video_music_provider['suffix']}"),
            )
            try:
                service.generate_bgm(
                    video_path=combined_video_path,
                    output_path=generated_bgm_path,
                    video_duration=audio_duration,
                    prompt=_get_video_music_prompt(params),
                )
                bgm_file_override = generated_bgm_path
            except video_music_provider["error_type"] as exc:
                # 视频、旁白和字幕都已生成时，第三方配乐临时失败不应浪费整条
                # 任务。当前视频明确禁用 BGM，并把降级结果返回 WebUI 提醒用户。
                logger.warning(
                    f"{display_name} BGM generation failed: task_id={task_id}, "
                    f"video_index={index}, error={exc}"
                )
                bgm_file_override = ""
                warnings.append({"code": warning_code, "video_index": index})

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        bgm_mix_succeeded = video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
            bgm_file_override=bgm_file_override,
        )
        if (
            video_music_provider is not None
            and bgm_file_override
            and not bgm_mix_succeeded
        ):
            # 第三方已成功返回并通过 FFmpeg 校验，但 MoviePy 最终混音仍可能
            # 因运行环境失败。视频服务会保留无 BGM 成片；API 生成失败时
            # override 为空，因此不会重复追加警告。
            warnings.append(
                {
                    "code": video_music_provider["warning_code"],
                    "video_index": index,
                }
            )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths, warnings


def _patch_cross_post_state(task_id: str, **kwargs) -> bool | None:
    """安全更新发布字段；短暂状态后端故障时有限重试。"""
    for attempt in range(1, _CROSS_POST_STATE_WRITE_ATTEMPTS + 1):
        try:
            return sm.state.patch_task(task_id, **kwargs)
        except Exception as exc:
            # Redis 短暂断连不应让任务永久停留在 pending/processing。发布状态
            # 写入频率很低，这里使用固定次数和短等待即可覆盖瞬时故障，同时
            # 避免后台线程无限阻塞。最后一次失败保留完整堆栈便于定位。
            if attempt >= _CROSS_POST_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    f"failed to update cross-post state after retries, "
                    f"task_id: {task_id}, fields: {', '.join(kwargs)}, "
                    f"attempts: {attempt}, error: {exc}"
                )
                return None

            logger.warning(
                f"retry cross-post state update, task_id: {task_id}, "
                f"fields: {', '.join(kwargs)}, attempt: {attempt}, error: {exc}"
            )
            time.sleep(_CROSS_POST_STATE_RETRY_DELAY_SECONDS)

    return None


def _record_cross_post_failure(
    task_id: str,
    error: Exception,
    results: list[dict] | None = None,
) -> None:
    """尽最大努力保存发布失败；状态后端不可用时由日志保留诊断信息。"""
    updated = _patch_cross_post_state(
        task_id,
        cross_post_state=const.CROSS_POST_STATE_FAILED,
        cross_post_results=results or None,
        cross_post_error=str(error),
        cross_post_owner=None,
    )
    if updated is False:
        logger.warning(f"discard cross-post failure for missing task: {task_id}")


def _ensure_cross_post_terminal_state(task_id: str) -> None:
    """Future 结束后把仍处于活动态的任务收敛为失败。"""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        # 此处已经是 Future 的最终回调，没有后续同步调用方可以处理异常。
        # 状态后端恢复后，下一次进程启动仍会通过恢复逻辑处理遗留状态。
        logger.exception(
            f"failed to verify final cross-post state, task_id: {task_id}, error: {exc}"
        )
        return

    if not task or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES:
        return

    logger.warning(
        f"cross-post worker ended without terminal state, task_id: {task_id}, "
        f"state: {task.get('cross_post_state')}"
    )
    _record_cross_post_failure(
        task_id,
        RuntimeError("cross-post worker ended without persisting a terminal state"),
        task.get("cross_post_results"),
    )


def recover_interrupted_cross_posts(page_size: int = 100) -> int | None:
    """
    将进程重启后无法恢复的发布任务标记为失败。

    跨平台发布使用当前进程内的线程池，不是持久化任务队列。进程启动时，
    Redis 中残留的 pending/processing 不会自动继续执行；如果继续把它们视为
    运行中，用户将永久无法删除任务。这里分页扫描状态，只处理当前进程没有
    对应 Future 的活动记录，并保留已经生成的视频结果。
    """
    recovered = 0
    page = 1

    while True:
        try:
            tasks, total = sm.state.get_all_tasks(page, page_size)
        except Exception as exc:
            logger.exception(f"failed to recover interrupted cross-post tasks: {exc}")
            return None

        for task in tasks:
            task_id = str(task.get("task_id") or "")
            if (
                not task_id
                or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES
                or _is_cross_post_active_in_process(task_id)
                or _is_cross_post_owner_alive(task.get("cross_post_owner"))
            ):
                continue

            updated = _patch_cross_post_state(
                task_id,
                cross_post_state=const.CROSS_POST_STATE_FAILED,
                cross_post_error=_INTERRUPTED_CROSS_POST_ERROR,
                cross_post_owner=None,
            )
            if updated is True:
                recovered += 1

        if page * page_size >= total or not tasks:
            break
        page += 1

    if recovered:
        logger.warning(f"recovered interrupted cross-post tasks: {recovered}")
    return recovered


def _run_cross_post(
    task_id: str,
    video_paths: tuple[str, ...],
    video_subject: str,
    video_script: str,
    video_language: str,
    platforms: tuple[str, ...],
    youtube_privacy_status: str,
) -> None:
    """后台执行跨平台发布，并只补充发布相关的任务字段。"""
    results = []
    try:
        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_PROCESSING,
            cross_post_error=None,
            cross_post_owner=_cross_post_process_owner,
        )
        if state_updated is not True:
            # False 表示任务已删除，None 表示状态后端暂时不可用。两种情况都
            # 不应继续调用第三方接口，否则用户无法查询或控制这次发布。
            if state_updated is False:
                logger.warning(f"skip cross-post for missing task: {task_id}")
            else:
                _record_cross_post_failure(
                    task_id,
                    RuntimeError("failed to persist cross-post processing state"),
                )
            return

        logger.info(
            f"cross-post started, task_id: {task_id}, platforms: {', '.join(platforms)}"
        )
        youtube_extra = None
        if any(platform.startswith("youtube") for platform in platforms):
            metadata = llm.generate_social_metadata(
                video_subject=video_subject,
                video_script=video_script,
                language=video_language or "",
                platform="youtube_shorts",
            )
            youtube_extra = {
                "youtube_title": metadata.get("title", video_subject),
                "youtube_description": metadata.get("caption", ""),
                "tags": metadata.get("hashtags", []),
                "privacyStatus": youtube_privacy_status,
                "containsSyntheticMedia": True,
            }

        for video_path in video_paths:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=video_subject or "Check out this video! #shorts #viral",
                platforms=list(platforms),
                youtube_extra=youtube_extra,
            )
            if not isinstance(result, dict):
                result = {
                    "success": False,
                    "error": "Upload-Post returned an invalid response",
                }
            results.append(result)

        failures = [result for result in results if not result.get("success")]
        if failures:
            error_messages = [
                str(
                    result.get("error")
                    or result.get("message")
                    or "unknown upload error"
                )
                for result in failures
            ]
            cross_post_state = const.CROSS_POST_STATE_FAILED
            cross_post_error = "; ".join(error_messages)
            logger.warning(
                f"cross-post completed with failures, task_id: {task_id}, "
                f"failed: {len(failures)}, total: {len(results)}"
            )
        else:
            cross_post_state = const.CROSS_POST_STATE_COMPLETE
            cross_post_error = None
            logger.success(
                f"cross-post completed, task_id: {task_id}, videos: {len(results)}"
            )

        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=cross_post_state,
            cross_post_results=results,
            cross_post_error=cross_post_error,
            cross_post_owner=None,
        )
        if state_updated is False:
            logger.warning(f"discard cross-post result for missing task: {task_id}")
        elif state_updated is None:
            # 上传已经结束但结果没有持久化时，不能继续保留 processing。
            # 失败状态写入会再次经过有限重试，至少让调用方得到明确终态。
            _record_cross_post_failure(
                task_id,
                RuntimeError("failed to persist final cross-post result"),
                results,
            )
    except Exception as exc:
        # 发布失败只影响发布状态，不能反向覆盖已经完成的视频任务。
        # 异常原文写入任务状态，API 调用方无需访问服务端日志也能定位问题。
        logger.exception(f"cross-post failed, task_id: {task_id}, error: {exc}")
        _record_cross_post_failure(task_id, exc, results)


def _run_cross_post_with_slot(*args) -> None:
    """执行发布任务，并确保成功、失败或异常时都会归还队列容量。"""
    try:
        _run_cross_post(*args)
    except Exception as exc:
        # _run_cross_post 已处理预期异常；这里是最后一道保护，避免未来新增
        # 逻辑抛出的异常只保存在无人读取的 Future 中。
        task_id = str(args[0]) if args else "unknown"
        logger.exception(
            f"cross-post worker crashed, task_id: {task_id}, error: {exc}"
        )
        if args:
            _record_cross_post_failure(task_id, exc)
    finally:
        _cross_post_slots.release()


def _finalize_cross_post_future(task_id: str, future: Future) -> None:
    """清理 Future 注册，并确保取消、异常和状态写入失败都能收敛。"""
    _unregister_cross_post_future(task_id, future)

    try:
        error = future.exception()
    except CancelledError:
        logger.warning(f"cross-post future was cancelled, task_id: {task_id}")
        # Future 在开始执行前被取消时，worker 的 finally 不会运行，因此需要
        # 在回调中归还队列容量，并把持久化状态改为失败。
        _cross_post_slots.release()
        _record_cross_post_failure(
            task_id,
            RuntimeError("cross-post job was cancelled before execution"),
        )
        return
    except Exception as exc:
        logger.exception(
            f"failed to inspect cross-post future, task_id: {task_id}, error: {exc}"
        )
        _ensure_cross_post_terminal_state(task_id)
        return

    if error is not None:
        logger.error(
            f"cross-post future failed, task_id: {task_id}, "
            f"error: {type(error).__name__}: {error}"
        )

    _ensure_cross_post_terminal_state(task_id)


def _schedule_cross_post(
    task_id: str,
    video_paths: list[str],
    params: VideoParams,
    video_script: str,
    platforms: list[str],
    youtube_privacy_status: str,
) -> str | None:
    """提交后台发布任务；成功返回 None，调度失败返回可查询的错误原因。"""
    if not _cross_post_slots.acquire(blocking=False):
        error = "cross-post queue is full; publishing was skipped"
        logger.warning(
            f"skip cross-post because queue is full, task_id: {task_id}, "
            f"capacity: {_cross_post_max_pending_tasks}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=error,
            cross_post_owner=None,
        )
        return error

    try:
        future = _cross_post_executor.submit(
            _run_cross_post_with_slot,
            task_id,
            tuple(video_paths),
            params.video_subject or "",
            video_script,
            params.video_language or "",
            tuple(platforms),
            youtube_privacy_status,
        )
        _register_cross_post_future(task_id, future)
        future.add_done_callback(partial(_finalize_cross_post_future, task_id))
    except RuntimeError as exc:
        _unregister_cross_post_future(task_id)
        _cross_post_slots.release()
        logger.exception(
            f"failed to schedule cross-post, task_id: {task_id}, error: {exc}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=f"failed to schedule cross-post: {exc}",
            cross_post_owner=None,
        )
        return f"failed to schedule cross-post: {exc}"

    return None


def _build_variant_script_prompt(params, video_index, video_count):
    """为多视频差异化构造变体提示，让同一主题生成不同角度的文案。

    只影响 video_index > 0 的后续视频：第一个视频保留用户原始 prompt，
    后续视频追加“换角度、避免重复”的引导，从源头上让 LLM 产出不同文案。
    """
    base = (getattr(params, "video_script_prompt", "") or "").strip()
    if video_index == 0 or video_count <= 1:
        return base
    variant_hint = (
        f"\n\n这是同一主题《{params.video_subject}》的第 {video_index + 1} 个视频版本，"
        f"请换一个不同的切入角度、结构和表述方式，"
        f"避免与前面 {video_index} 个版本的内容重复。"
    )
    return f"{base}\n{variant_hint}".strip()


def _generate_variant_script(task_id, params, video_index, video_count):
    """为第 video_index 个视频独立生成文案（脚本）。

    用户手填了脚本时无法差异化，所有视频复用同一份脚本。
    """
    logger.info(
        f"\n\n## generating variant script {video_index + 1}/{video_count}"
    )
    if params.video_script.strip():
        return params.video_script.strip(), {
            "used": False, "fallback": False, "chunks": 0, "empty": False
        }
    video_script, kb_info = llm.generate_script(
        video_subject=params.video_subject,
        language=params.video_language,
        paragraph_number=params.paragraph_number,
        video_script_prompt=_build_variant_script_prompt(
            params, video_index, video_count
        ),
        custom_system_prompt=params.custom_system_prompt,
        use_knowledge=getattr(params, "use_knowledge", False),
        kb_doc_filenames=getattr(params, "kb_doc_filenames", None),
        target_duration=getattr(params, "video_script_duration", 0) or 0,
    )
    return video_script, kb_info


def _run_multi_video_variants(task_id, params, voice_preview, topic_terms):
    """多视频差异化：每个视频独立文案，共享同一批素材。"""
    video_count = params.video_count

    # 1. 循环生成 N 个不同文案
    scripts = []
    kb_infos = []
    for i in range(video_count):
        _wait_if_paused(task_id)
        script, kb_info = _generate_variant_script(
            task_id, params, i, video_count
        )
        if not script or "Error: " in script:
            error = (
                script.removeprefix("Error: ").strip()
                if isinstance(script, str) and "Error: " in script
                else "failed to generate video script"
            )
            return _mark_task_failed(task_id, "script", error)
        scripts.append(script)
        kb_infos.append(kb_info)
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=int(10 + 15 * (i + 1) / video_count),
        )
        cp.save(task_id, "script", int(10 + 15 * (i + 1) / video_count))

    # 2. 用第一个文案生成搜索词（素材共享，仅检索一次）
    video_terms = ""
    if params.video_source != "local":
        if params.video_source == "knowledge_base" and topic_terms:
            video_terms = topic_terms
            logger.info(
                f"using topic-anchored KB terms ({len(video_terms)}): "
                f"{video_terms[:5]}..."
            )
        else:
            video_terms = generate_terms(task_id, params, scripts[0])
            if not video_terms:
                return _mark_task_failed(
                    task_id, "terms", "failed to generate video search terms"
                )

    save_script_data(
        task_id, scripts[0], video_terms, params, topic_terms=topic_terms
    )
    _wait_if_paused(task_id)

    # 3. 循环生成配音
    audio_files = []
    audio_durations = []
    sub_makers = []
    for i, script in enumerate(scripts):
        _wait_if_paused(task_id)
        audio_file, audio_duration, sub_maker = generate_audio(
            task_id, params, script,
            voice_preview=voice_preview, variant_index=i + 1,
        )
        if not audio_file:
            return _mark_task_failed(
                task_id, "audio", "failed to prepare narration audio"
            )
        audio_files.append(audio_file)
        audio_durations.append(audio_duration)
        sub_makers.append(sub_maker)
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=int(25 + 15 * (i + 1) / video_count),
        )

    # 4. 循环生成字幕
    subtitle_paths = []
    for i, script in enumerate(scripts):
        _wait_if_paused(task_id)
        subtitle_path = generate_subtitle(
            task_id, params, script, sub_makers[i], audio_files[i],
            variant_index=i + 1,
        )
        subtitle_paths.append(subtitle_path)
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=int(40 + 5 * (i + 1) / video_count),
        )

    # 5. 素材下载一次（共享），覆盖所有视频总时长
    total_duration = sum(audio_durations)
    downloaded_videos = get_video_materials(
        task_id, params, video_terms, total_duration / video_count,
        kb_fallback_to_pexels=False,
        kb_category=getattr(params, "kb_category", "") or "",
    )
    if downloaded_videos is None:
        return _mark_task_failed(
            task_id, "materials", "failed to prepare video materials"
        )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)
    cp.save(task_id, "materials", 50)
    _wait_if_paused(task_id)

    # 6. 合成（generate_final_videos 内部按 video_count 循环）
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    final_video_paths, combined_video_paths, generation_warnings = generate_final_videos(
        task_id,
        params,
        downloaded_videos,
        audio_files,
        subtitle_paths,
        audio_durations,
        clip_durations=None,
    )

    if not final_video_paths:
        return _mark_task_failed(
            task_id, "video", "failed to generate final video"
        )

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos "
        f"with distinct scripts."
    )
    cp.save(task_id, "complete", 100)
    # Quality scoring (Level 3.5)
    from app.services.quality import score_task as _score_task, save_quality_report as _save_qr
    try:
        _quality_reports = _score_task(
            task_id, final_video_paths, audio_duration=total_duration
        )
        _save_qr(task_id, _quality_reports)
    except Exception as _qe:
        logger.warning(f"Quality scoring failed (non-fatal): {_qe}")

    # 跨平台发布：多视频共用同一套发布配置，脚本描述取第一个版本。
    cross_post_enabled = (
        upload_post.upload_post_service.is_configured()
        and upload_post.upload_post_service.auto_upload
    )
    platforms = (
        list(upload_post.upload_post_service.platforms)
        if cross_post_enabled
        else []
    )
    should_cross_post = cross_post_enabled and bool(platforms)
    cross_post_state = const.CROSS_POST_STATE_PENDING if should_cross_post else None

    _fallback_kb_info = {"used": False, "fallback": False, "chunks": 0, "empty": False}
    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": scripts,
        "terms": video_terms,
        "audio_file": audio_files,
        "audio_duration": audio_durations,
        "subtitle_path": subtitle_paths,
        "materials": downloaded_videos,
        "cross_post_state": cross_post_state,
        "cross_post_results": None,
        "cross_post_error": None,
        "cross_post_owner": _cross_post_process_owner if should_cross_post else None,
        "warnings": generation_warnings or None,
        "kb_info": kb_infos[0] if kb_infos else _fallback_kb_info,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )

    if should_cross_post:
        scheduling_error = _schedule_cross_post(
            task_id=task_id,
            video_paths=final_video_paths,
            params=params,
            video_script=scripts[0],
            platforms=platforms,
            youtube_privacy_status=(
                upload_post.upload_post_service.youtube_privacy_status
            ),
        )
        if scheduling_error:
            kwargs["cross_post_state"] = const.CROSS_POST_STATE_FAILED
            kwargs["cross_post_error"] = scheduling_error
            kwargs["cross_post_owner"] = None

    return kwargs


def _run_pipeline(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    voice_preview: dict | None = None,
):
    _set_trace_id()
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)
    cp.save(task_id, "start", 5, video_subject=getattr(params, "video_subject", "") or "")

    # 只有完整成片流程需要视频配乐供应商。尽早阻止缺少 Key 的完整任务，避免
    # 先消耗 LLM、TTS 和素材服务额度；中间产物接口仍可独立使用。
    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_enabled = (
        stop_at == "video"
        and video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    if video_music_enabled:
        service = video_music_provider["service"]
        display_name = video_music_provider["display_name"]
        if not service.is_enabled():
            return _mark_task_failed(
                task_id,
                "preflight",
                f"{display_name} background music requires an API key",
            )

        # WebUI 会限制输入长度，但 API、CLI 和历史任务可以绕过前端控件。
        # 在生成脚本、配音和素材之前按供应商上限再次校验，避免完整视频合成后
        # 才由第三方请求拒绝。服务层仍保留同一校验，作为直接调用时的最后防线。
        music_prompt = _get_video_music_prompt(params)
        max_prompt_length = int(getattr(service, "MAX_PROMPT_LENGTH", 0) or 0)
        if max_prompt_length and len(music_prompt) > max_prompt_length:
            return _mark_task_failed(
                task_id,
                "preflight",
                (f"{display_name} music prompt exceeds {max_prompt_length} characters"),
            )

        # 供应商可以选择提供不计费的账号前置检查。检查函数只应抛出确定性
        # 错误；网络波动或权限范围无法确认时由服务层记录警告并继续实际生成。
        validate_access = getattr(service, "validate_generation_access", None)
        if callable(validate_access):
            try:
                validate_access()
            except video_music_provider["error_type"] as exc:
                return _mark_task_failed(task_id, "preflight", str(exc))

    # KB 脚本生成和 KB 素材搜索已分离为独立开关：
    # - use_knowledge: 控制 LLM 脚本生成时是否注入 KB 上下文
    # - video_source:  独立控制素材来源 (pexels / knowledge_base / local)
    # 不再自动切换 video_source，允许 "KB 写脚本 + Pexels 配画面" 混合模式。

    # 主题搜索词：KB 素材模式下用 jieba 分词 + 简洁降级，避免 LLM 主题分解产生
    # 过于抽象的搜索词（如"反渗透膜更换"）无法匹配 KB 中的实际文件名。
    topic_terms = []
    if params.video_source == "knowledge_base":
        logger.info("KB material mode: extracting keywords from video_subject")
        import jieba as _jieba
        _raw_words = list(_jieba.cut(params.video_subject))
        topic_terms = [w.strip() for w in _raw_words if len(w.strip()) >= 3]
        # 完整主题词优先插入到列表头部，它比短词更容易命中 KB 实际内容
        _full_subject = params.video_subject.strip()
        if _full_subject and _full_subject not in topic_terms:
            topic_terms.insert(0, _full_subject)
        if topic_terms:
            logger.info(f"jieba keyword extraction: {len(topic_terms)} terms -> {topic_terms[:10]}")
        if not topic_terms:
            # 兜底：LLM 分解作为后备
            logger.info("jieba returned empty, falling back to LLM topic decomposition")
            topic_terms = llm.decompose_topic(
                video_subject=params.video_subject,
                language=getattr(params, "video_language", "zh") or "zh",
            )
            if topic_terms:
                logger.info(f"topic decomposition: {len(topic_terms)} terms generated")
            else:
                logger.warning("topic decomposition returned empty, will fall back to script-based terms")

    # 1. Generate script
    _material_driven = bool(
        getattr(params, "material_driven_mode", False)
        and getattr(params, "selected_category", None)
    )
    if _material_driven:
        # 素材驱动模式复用分镜的逐场景时长提取/顺序拼接/裁剪语义
        params.match_materials_to_script = True

    _grounded_kb = (
        params.video_source == "knowledge_base"
        and params.match_materials_to_script
        and not params.video_script.strip()
        and not _material_driven
    )
    _grounded_storyboard = None

    # 多视频差异化：普通模式（非即梦 / 非素材驱动 / 非 grounded KB）下，
    # 每个视频独立生成文案，共享同一批素材。
    if (
        params.video_count > 1
        and stop_at == "video"
        and params.video_source != "jimeng"
        and not _material_driven
        and not _grounded_kb
    ):
        return _run_multi_video_variants(
            task_id, params, voice_preview, topic_terms
        )

    if _grounded_kb:
        # 素材优先链路：选中分类 → 目录直列素材 → 按素材写脚本，
        # 实现文案 ↔ 素材 一一对应、根治素材漂移（参考素材驱动脚本模式）。
        # 未选分类时保留语义搜索回退（无目录可锚定）。
        _kb_cat = getattr(params, "kb_category", "") or ""
        kb_media = []
        try:
            if _kb_cat:
                kb_media = kb_client.list_media_sampled(_kb_cat)
                logger.info(
                    f"grounded KB (material-first): {len(kb_media)} media from "
                    f"category '{_kb_cat}'"
                )
            else:
                kb_media = kb_client.relevant_media(
                    getattr(params, "video_subject", "") or "",
                    top_k=40,
                    category="",
                )
                logger.info(f"grounded KB: {len(kb_media)} relevant media retrieved")
        except Exception as _e:
            logger.warning(f"grounded KB: media fetch failed: {_e}")

        if kb_media:
            _knowledge_context = ""
            if getattr(params, "use_knowledge", False):
                try:
                    _kres = kb_client.search_knowledge(params.video_subject, top_k=8)
                    if _kres:
                        _parts = []
                        for _i, _chunk in enumerate(_kres, 1):
                            _meta = _chunk.get("metadata", {})
                            _parts.append(
                                f"[来源 {_i}: {_meta.get('filename', 'unknown')}]\n"
                                f"{_chunk.get('content', '')}"
                            )
                        _knowledge_context = "\n\n".join(_parts)
                except Exception as _e:
                    logger.warning(f"grounded KB: knowledge fetch failed: {_e}")

            if _kb_cat:
                # 素材优先：素材清单是主驱动，按素材写脚本
                _grounded_storyboard = llm.generate_script_from_materials(
                    kb_media=kb_media,
                    video_subject=params.video_subject,
                    language=params.video_language,
                    paragraph_number=params.paragraph_number,
                    video_script_prompt=params.video_script_prompt,
                    custom_system_prompt=params.custom_system_prompt,
                    target_duration=getattr(params, "video_script_duration", 0) or 0,
                    knowledge_context=_knowledge_context,
                )
            else:
                # 无分类：语义检索 + storyboard（现状回退）
                _grounded_storyboard = llm.generate_script_with_storyboard(
                    video_subject=params.video_subject,
                    language=params.video_language,
                    paragraph_number=params.paragraph_number,
                    video_script_prompt=params.video_script_prompt,
                    custom_system_prompt=params.custom_system_prompt,
                    knowledge_context=_knowledge_context,
                    kb_media=kb_media,
                )

        if _grounded_storyboard:
            _script_parts = [
                str(s.get("text", "")).strip()
                for s in _grounded_storyboard
                if str(s.get("text", "")).strip()
            ]
            video_script = "\n\n".join(_script_parts).strip()
            kb_info = {
                "used": True, "fallback": False,
                "chunks": len(kb_media), "empty": False,
            }
            if not video_script:
                _grounded_storyboard = None
        else:
            logger.warning("grounded KB: generation empty, falling back to legacy flow")

    if _material_driven and not _grounded_storyboard:
        _kb_media = _build_kb_media_from_category(
            params.selected_category
        )
        if _kb_media:
            if params.video_script.strip():
                # 用户已编辑脚本 → 在勾选素材集合内重新匹配
                _grounded_storyboard = llm.reassign_media_to_script(
                    video_script=params.video_script,
                    kb_media=_kb_media,
                )
            else:
                # 无脚本 → 素材驱动生成脚本
                _grounded_storyboard = llm.generate_script_from_materials(
                    kb_media=_kb_media,
                    video_subject=params.video_subject,
                    language=params.video_language,
                    paragraph_number=params.paragraph_number,
                    video_script_prompt=params.video_script_prompt,
                    custom_system_prompt=params.custom_system_prompt,
                    target_duration=getattr(params, "video_script_duration", 0) or 0,
                )
        if _grounded_storyboard:
            if params.video_script.strip():
                video_script = params.video_script
            else:
                _parts = [
                    str(s.get("text", "")).strip()
                    for s in _grounded_storyboard
                    if str(s.get("text", "")).strip()
                ]
                video_script = "\n\n".join(_parts).strip()
            kb_info = {
                "used": True, "fallback": False,
                "chunks": len(_kb_media), "empty": False,
            }
        else:
            logger.warning(
                "material-driven: no storyboard, falling back to legacy flow"
            )

    _jimeng_shots = None
    if params.video_source == "jimeng":
        # 前端预览并编辑过的分镜（含画面提示词）优先复用，避免重复取图/识图/DeepSeek 创作
        _reused_shots = _parse_frontend_jimeng_storyboard(
            getattr(params, "jimeng_storyboard", "") or ""
        )
        if _reused_shots:
            _jimeng_shots = _reuse_jimeng_storyboard(task_id, _reused_shots)
            if not _jimeng_shots:
                return _mark_task_failed(
                    task_id, "materials",
                    "即梦模式：复用分镜的图片下载失败，请重新生成宣传片分镜脚本",
                )
            video_script = (params.video_script or "").strip()
        else:
            # 即梦模式：先 Kimi 识图（客观视觉描述），再 DeepSeek 统一创作
            # 逐镜头画面提示词 + 分镜口播，确保口播贴合画面且总时长 ≤30s。
            _jimeng_shots = _generate_jimeng_storyboard(
                task_id=task_id,
                params=params,
                kb_category=getattr(params, "kb_category", "") or "",
            )
            if not _jimeng_shots:
                return _mark_task_failed(
                    task_id, "materials",
                    "即梦模式：知识库选图或 Kimi 识图失败，请检查系列图片与 moonshot_api_key",
                )
            _existing_script = (params.video_script or "").strip()
            if _existing_script:
                # 前端已预览并（可能）编辑过口播脚本：复用脚本，只重新生成运镜提示词
                _, _jimeng_shots = llm.generate_jimeng_storyboard_and_script(
                    video_subject=params.video_subject,
                    shots=_jimeng_shots,
                    language=params.video_language,
                    reuse_script=_existing_script,
                )
                video_script = _existing_script
            else:
                video_script, _jimeng_shots = llm.generate_jimeng_storyboard_and_script(
                    video_subject=params.video_subject,
                    shots=_jimeng_shots,
                    language=params.video_language,
                )
        if not video_script:
            return _mark_task_failed(task_id, "script", "即梦模式：分镜脚本/口播为空")
        kb_info = {"used": False, "fallback": False, "chunks": 0, "empty": False}
    elif not _grounded_storyboard:
        video_script, kb_info = generate_script(task_id, params)
    if not video_script or "Error: " in video_script:
        error = (
            video_script.removeprefix("Error: ").strip()
            if isinstance(video_script, str) and "Error: " in video_script
            else "failed to generate video script"
        )
        return _mark_task_failed(task_id, "script", error)

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)
    cp.save(task_id, "script", 10)
    _wait_if_paused(task_id)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100,
            script=video_script, kb_info=kb_info,
        )
        return {"script": video_script, "kb_info": kb_info}

    # 2. Generate terms
    video_terms = ""
    if params.video_source not in ("local", "jimeng"):
        # KB 模式优先用主题分解词，其次用脚本关键词
        if params.video_source == "knowledge_base" and topic_terms:
            video_terms = topic_terms
            logger.info(f"using topic-anchored KB terms ({len(video_terms)}): {video_terms[:5]}...")
        else:
            video_terms = generate_terms(task_id, params, video_script)
            if not video_terms:
                return _mark_task_failed(
                    task_id,
                    "terms",
                    "failed to generate video search terms",
                )

    # 分镜模式：为每段文案生成中英文搜索关键词
    _storyboard = None
    if _grounded_storyboard:
        _storyboard = _grounded_storyboard
        logger.info(
            f"grounded KB: using {len(_storyboard)} storyboard shots "
            f"with pre-assigned media"
        )
    elif params.match_materials_to_script and isinstance(video_terms, list):
        _storyboard = llm.generate_storyboard(
            video_subject=params.video_subject,
            video_script=video_script,
        )
        if _storyboard:
            logger.info(
                f"storyboard: {len(_storyboard)} shots generated, "
                f"using per-shot keywords for material matching"
            )
            _cn_terms = []
            _en_terms = []
            for shot in _storyboard:
                _cn_terms.extend(shot.get("keywords_cn", []))
                _en_terms.extend(shot.get("keywords_en", []))
            logger.info(
                f"storyboard keywords: {len(_cn_terms)} CN terms, {len(_en_terms)} EN terms"
            )
            # KB 模式：保留分镜关键词用于逐场景素材匹配
            # per-shot keywords_cn 保留在 _storyboard 中，
            # 素材下载阶段按场景逐个搜索，实现文案-画面一一对应
            if params.video_source == "knowledge_base":
                logger.info(
                    f"KB mode: using per-shot storyboard keywords for "
                    f"scene-by-scene material matching ({len(_storyboard)} shots)"
                )
            else:
                if _en_terms:
                    video_terms = _en_terms
                logger.info(
                    f"using storyboard keywords for material matching: "
                    f"{len(video_terms)} terms"
                )
        else:
            logger.warning("storyboard generation returned empty, using original terms")

    save_script_data(
        task_id, video_script, video_terms, params,
        storyboard=_jimeng_shots if params.video_source == "jimeng" else _storyboard,
        topic_terms=topic_terms,
    )
    cp.save(task_id, "terms", 20)
    _wait_if_paused(task_id)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio
    audio_file, audio_duration, sub_maker = generate_audio(
        task_id,
        params,
        video_script,
        voice_preview=voice_preview,
    )
    if not audio_file:
        return _mark_task_failed(
            task_id,
            "audio",
            "failed to prepare narration audio",
        )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)
    cp.save(task_id, "audio", 30)
    _wait_if_paused(task_id)

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    subtitle_path = generate_subtitle(
        task_id, params, video_script, sub_maker, audio_file
    )

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)
    cp.save(task_id, "subtitle", 40)
    _wait_if_paused(task_id)

    # 提取分镜时长（开启按顺序匹配时生效）
    _clip_durations = None
    _para_debug = [p for p in video_script.split(chr(10)+chr(10)) if p.strip()]
    if not _para_debug:
        _para_debug = [p for p in video_script.split(chr(10)) if p.strip()]
    logger.info(
        f"shot-by-shot check: match_materials={params.match_materials_to_script}, "
        f"paragraphs={len(_para_debug)}, terms={len(video_terms) if isinstance(video_terms, list) else 0}"
    )
    if params.match_materials_to_script:
        _clip_durations = _parse_paragraph_durations(subtitle_path, video_script)
        if _clip_durations:
            logger.info(
                f"shot-by-shot: {len(_clip_durations)} paragraph durations "
                f"extracted, total: {sum(_clip_durations):.2f}s"
            )
        else:
            # 单段落回退：按场景数均分音频时长
            _ref_count = len(_storyboard) if _storyboard else (
                len(video_terms) if isinstance(video_terms, list) else 0
            )
            if _ref_count > 0:
                _per_shot = audio_duration / _ref_count
                _clip_durations = [_per_shot] * _ref_count
                logger.info(
                    f"shot-by-shot fallback: {_ref_count} scenes, "
                    f"{_per_shot:.2f}s each"
                )

    # ── 系列预检：知识库素材不足时中止（不漂移）──
    if params.video_source in ("knowledge_base", "jimeng"):
        _kb_cat = getattr(params, "kb_category", "") or ""
        if _kb_cat:
            _pre = kb_client.precheck_series(_kb_cat, min_assets=6)
            if params.video_source == "jimeng":
                # 即梦模式只需图片（图生视频首帧），至少 6 张图
                _enough = _pre.get("images", 0) >= 6
            else:
                _enough = _pre.get("sufficient", False)
            if not _enough:
                return _mark_task_failed(
                    task_id, "materials",
                    f"系列素材不足：'{_kb_cat}' 仅 {_pre.get('total', 0)} 个素材"
                    f"（图片 {_pre.get('images', 0)} / 视频 {_pre.get('videos', 0)}），"
                    f"至少需要 {_pre.get('min_assets', 6)} 个。请补充素材或更换系列。",
                )

    # ── 即梦模式预检：视觉模型与即梦 key 必须就绪 ──
    if params.video_source == "jimeng":
        if not vision_client.is_enabled():
            return _mark_task_failed(
                task_id, "materials",
                "即梦模式需要 Kimi 视觉模型（moonshot_api_key 未配置，请在 config.toml 补充）",
            )
        if not jimeng_client.is_enabled():
            return _mark_task_failed(
                task_id, "materials",
                "即梦模式需要即梦视频生成（volcengine_api_key 未配置，请在 config.toml 补充）",
            )

    # 5. Get video materials
    if _grounded_storyboard:
        _media_filenames = [s.get("media", "") for s in _grounded_storyboard]
        downloaded_videos = material.download_media_by_filenames(
            task_id=task_id,
            filenames=_media_filenames,
            kb_category=getattr(params, "kb_category", "") or "",
        )
    elif _storyboard and params.video_source == "knowledge_base":
        # 分镜模式 KB：按场景逐个下载素材，实现文案-画面一一对应
        # 每个 shot 独立执行 3 层降级搜索（精准→宽泛→邻居复用）
        downloaded_videos = material.download_videos_by_storyboard(
            task_id=task_id,
            storyboard=_storyboard,
            video_subject=params.video_subject,
            audio_duration=audio_duration,
            max_clip_duration=params.video_clip_duration,
            clip_durations=_clip_durations,
            kb_category=getattr(params, "kb_category", "") or "",
        )
        # download_videos_by_storyboard 内部已处理降级和邻居复用，
        # 返回 [] 表示所有场景均失败，由下方空素材处理逻辑接管
    elif params.video_source == "jimeng":
        # 即梦模式：基于识图结果 + DeepSeek 画面提示词 → 即梦图生视频片段
        downloaded_videos = _generate_jimeng_videos(
            task_id=task_id,
            params=params,
            shots=_jimeng_shots,
        )
    else:
        downloaded_videos = get_video_materials(
            task_id, params, video_terms, audio_duration,
            kb_fallback_to_pexels=False,
            kb_category=getattr(params, "kb_category", "") or "",
        )
    # ── 分镜时长重分配：按实际素材数量均分音频时长 ──
    if _clip_durations and downloaded_videos:
        MIN_SHOT_DURATION = 2.0
        _n = len(downloaded_videos)
        _idle = len(_storyboard) if _storyboard else (
            len(video_terms) if isinstance(video_terms, list) else 0
        )
        if _n < _idle or _n < len(_clip_durations):
            _per = max(audio_duration / _n, MIN_SHOT_DURATION)
            _clip_durations = [_per] * _n
            logger.info(
                f"shot durations redistributed: {_n} materials × {_per:.2f}s "
                f"(was {_idle} search terms → {len(_clip_durations)} expected slots)"
            )
    if not downloaded_videos:
        # KB 模式允许空素材列表，视频组装层会生成占位画面循环复用
        if params.video_source == "knowledge_base":
            logger.warning(
                "KB returned no materials, video will use placeholder clips. "
                "Consider starting the KB server."
            )
        elif params.video_source == "jimeng":
            return _mark_task_failed(
                task_id,
                "materials",
                "即梦图生视频失败：系列图片不足或视觉模型/即梦生成异常，请检查日志",
            )
        else:
            return _mark_task_failed(
                task_id,
                "materials",
                "failed to prepare video materials",
            )

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
        )
        return {"materials": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)
    cp.save(task_id, "materials", 50)
    _wait_if_paused(task_id)

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    final_video_paths, combined_video_paths, generation_warnings = generate_final_videos(
        task_id,
        params,
        downloaded_videos,
        [audio_file],
        [subtitle_path],
        [audio_duration],
        clip_durations=_clip_durations,
    )

    if not final_video_paths:
        return _mark_task_failed(
            task_id,
            "video",
            "failed to generate final video",
        )

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )
    cp.save(task_id, "complete", 100)
    # Quality scoring (Level 3.5)
    from app.services.quality import score_task as _score_task, save_quality_report as _save_qr
    try:
        _audio_dur = audio_duration if "audio_duration" in dir() else 0
        _quality_reports = _score_task(task_id, final_video_paths, audio_duration=_audio_dur)
        _save_qr(task_id, _quality_reports)
    except Exception as _qe:
        logger.warning(f"Quality scoring failed (non-fatal): {_qe}")

    # 7. 先完成视频生成任务，再按需提交跨平台发布。第三方上传可能耗时
    # 数分钟，不应阻塞视频结果返回，也不能反向影响已经生成的成片。
    cross_post_enabled = (
        upload_post.upload_post_service.is_configured()
        and upload_post.upload_post_service.auto_upload
    )
    platforms = (
        list(upload_post.upload_post_service.platforms)
        if cross_post_enabled
        else []
    )
    should_cross_post = cross_post_enabled and bool(platforms)
    if cross_post_enabled and not platforms:
        logger.warning(
            f"skip cross-post because no platforms are configured, task_id: {task_id}"
        )
    cross_post_state = const.CROSS_POST_STATE_PENDING if should_cross_post else None

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
        "cross_post_state": cross_post_state,
        "cross_post_results": None,
        "cross_post_error": None,
        "cross_post_owner": _cross_post_process_owner if should_cross_post else None,
        "warnings": generation_warnings or None,
        "kb_info": kb_info,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )

    if should_cross_post:
        scheduling_error = _schedule_cross_post(
            task_id=task_id,
            video_paths=final_video_paths,
            params=params,
            video_script=video_script,
            platforms=platforms,
            youtube_privacy_status=(
                upload_post.upload_post_service.youtube_privacy_status
            ),
        )
        # 队列满或线程池关闭属于同步可知的调度失败。任务状态已经由调度函数
        # 更新，这里同步修正返回快照，避免调用方收到与后续查询不一致的 pending。
        if scheduling_error:
            kwargs["cross_post_state"] = const.CROSS_POST_STATE_FAILED
            kwargs["cross_post_error"] = scheduling_error
            kwargs["cross_post_owner"] = None

    return kwargs


def start(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    voice_preview: dict | None = None,
):
    """执行任务流水线，并确保未预期异常也会转换成可查询的失败状态。"""
    try:
        return _run_pipeline(
            task_id,
            params,
            stop_at=stop_at,
            voice_preview=voice_preview,
        )
    except Exception as exc:
        logger.exception(
            f"unexpected task pipeline failure, task_id: {task_id}, error: {exc}"
        )
        return _mark_task_failed(
            task_id,
            "pipeline",
            f"{type(exc).__name__}: {exc}",
        )


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
