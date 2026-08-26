import os
import random
import threading
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.utils import utils
from app.services.kb_client import kb_client
import jieba

from app.services import llm

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材搜索和下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]




# -- Pexels API global rate limiter --
class _RateLimiter:
    def __init__(self, max_per_second=2.0):
        import time
        self._lock = threading.Lock()
        self._min_interval = 1.0 / max_per_second
        self._last_call = 0.0
        self._time = time.time

    def acquire(self):
        with self._lock:
            now = self._time()
            wait = self._last_call + self._min_interval - now
            if wait > 0:
                import time
                time.sleep(wait)
            self._last_call = self._time()

_pexels_rate_limiter = _RateLimiter(max_per_second=2.0)
def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    _pexels_rate_limiter.acquire()
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if w == video_width and h == video_height:
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Coverr (https://coverr.co) - free HD/4K stock videos,
    subject to Coverr license terms (https://coverr.co/license).

    Coverr API notes (based on official docs at api.coverr.co/docs/):
      - 鉴权: Authorization: Bearer <api_key>
      - 搜索端点: GET /videos?query=...,响应结构 {"hits": [...], ...}
      - 加 ?urls=true 在搜索响应里直接返回 mp4 直链
      - URL 是 signed JWT(绑定 API key,无过期时间)
      - Coverr 库以 16:9 横屏为主,9:16 portrait 占比极低(约 1%)
        因此本函数不做 aspect_ratio 过滤,由下游 video.py 的
        resize + letterbox 逻辑统一处理
      - duration 字段同时存在 number 和 string 两种形态,本函数都接受

    本函数使用 urls.mp4_download 字段作为下载地址 —— 按 Coverr 官方文档
    (https://api.coverr.co/docs/videos/#download-a-video) 的说法,
    GET 这个 URL 本身就被 Coverr 当作一次合法的 download 事件计入统计,
    无需再调用 PATCH /videos/:id/stats/downloads。
    """
    api_key = get_api_key("coverr_api_keys")
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "query": search_term,
        "page_size": 20,
        "urls": "true",
        "sort": "popular",
    }
    query_url = f"https://api.coverr.co/videos?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items: List[MaterialInfo] = []

        if not isinstance(response, dict) or "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items

        for v in response["hits"]:
            # duration 在不同响应里可能是 number(11.625) 或 string("10.500000")
            try:
                duration = int(float(v.get("duration") or 0))
            except (TypeError, ValueError):
                continue
            if duration < minimum_duration:
                continue

            video_id = v.get("id")
            mp4_download_url = (v.get("urls") or {}).get("mp4_download")
            if not video_id or not mp4_download_url:
                continue

            item = MaterialInfo()
            item.provider = "coverr"
            item.url = mp4_download_url
            item.duration = duration
            video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # if video does not exist, download it
    with open(video_path, "wb") as f:
        f.write(
            requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(60, 240),
            ).content
        )

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        clip = None
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
            try:
                os.remove(video_path)
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {video_path}, error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {video_path}, error: {str(close_error)}"
                    )
    return ""


def _download_files_from_kb(
    task_id: str,
    search_terms: List[str],
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
    category: str = "",
) -> List[str]:
    """从知识库下载媒体文件作为视频素材。

    对每个搜索词调用 kb-app 的混合搜索（语义+关键词）。
    降级策略（jieba 分词等）已移至 kb-app 服务端处理，
    客户端只需调用一次即可获得最佳结果。
    图片会被后续 video.py 中的 Ken Burns 效果处理为视频片段。
    """
    material_directory = utils.task_dir(task_id)
    os.makedirs(material_directory, exist_ok=True)

    downloaded = []
    total_duration_estimate = 0.0
    seen_names = set()

    if not kb_client.is_healthy():
        logger.warning("knowledge base unreachable for material search, falling back")
        return []

    _one_per_term = match_script_order

    _diversity_hard_cap = max(audio_duration * 5, 90)
    for search_term in search_terms:

        _top_k = 6  # fetch more per term for better diversity
        media_results = _search_kb_with_fallback(search_term, top_k=_top_k, category=category)

        for item in media_results:
            name = item.get("name", "")
            if name in seen_names:
                continue
            seen_names.add(name)

            local_path = kb_client.download_media(name, material_directory)
            if local_path:
                downloaded.append(local_path)
                # 分镜模式用实际槽位时长估算（避免高估导致不触发Pexels降级）
                _per_slot = (
                    audio_duration / len(search_terms)
                    if match_script_order and search_terms
                    else max_clip_duration
                )
                if name.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
                    try:
                        clip = VideoFileClip(local_path)
                        total_duration_estimate += min(clip.duration, _per_slot)
                        clip.close()
                    except Exception:
                        total_duration_estimate += _per_slot
                else:
                    total_duration_estimate += _per_slot

                logger.info(
                    f"kb media downloaded: {name}, "
                    f"total estimate: {total_duration_estimate:.1f}s / {audio_duration:.1f}s"
                )

                if total_duration_estimate >= _diversity_hard_cap:
                    break

    logger.success(
        f"kb media: downloaded {len(downloaded)} files, "
        f"duration estimate: {total_duration_estimate:.1f}s"
    )
    return downloaded


def _search_kb_with_fallback(search_term: str, top_k: int = 5, category: str = "") -> list:
    """搜索 KB 媒体（图片+视频）。

    降级策略（完整词 → jieba分词 → bigram）已移至 kb-app 服务端。
    客户端只需调用一次 search_media 即可。
    """
    logger.info(f"searching kb media for: '{search_term}'")
    results = kb_client.search_media(search_term, top_k=top_k, file_type="all", category=category)
    if results:
        _log_kb_results(search_term, results, "hybrid")
        return results

    # hybrid search empty, fall back to image-only search
    logger.info(f"kb hybrid empty for '{search_term}', trying image-only fallback")
    results = kb_client.search_media(search_term, top_k=top_k, file_type="image", category=category)
    if results:
        _log_kb_results(search_term, results, "image_fallback")
    else:
        logger.warning(f"kb search '{search_term}': no media found (image fallback also empty)")
    return results


def _log_kb_results(search_term: str, results: list, level: str):
    """记录 KB 搜索结果日志（文件类型分布）。"""
    type_counts = {"video": 0, "image": 0, "other": 0}
    for item in results:
        name = item.get("name", "")
        if name.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
            type_counts["video"] += 1
        elif name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")):
            type_counts["image"] += 1
        else:
            type_counts["other"] += 1
    logger.info(
        f"kb search [{level}] '{search_term}': {len(results)} results, "
        f"video={type_counts['video']}, image={type_counts['image']}, other={type_counts['other']}"
    )


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
    kb_fallback_to_pexels: bool = False,
    kb_category: str = "",
) -> List[str]:
    # ── 知识库素材分支（混合模式：KB 优先，不够时 Pexels 补充）──
    if source == "knowledge_base":
        # 知识库语义搜索需要中文查询词。
        # 翻译失败时降级使用原始词，不中断任务。
        _cn_terms = search_terms
        try:
            _cn = llm.translate_terms(search_terms)
            if _cn and any(_cn):
                _cn_terms = _cn
                logger.info(
                    f"kb search terms translated: {search_terms} -> {_cn_terms}"
                )
        except Exception as _e:
            logger.warning(f"kb term translation failed, using original: {_e}")

        result = _download_files_from_kb(
            task_id=task_id,
            search_terms=_cn_terms,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            match_script_order=match_script_order,
            category=kb_category,
        )

        if not result:
            logger.warning("=== CODE-V3: KB branch active, no Pexels fallback ===")
            logger.warning("knowledge base returned no media. Video assembly will loop materials.")
            return []

        # 计算知识库素材已覆盖的时长，不足部分用 Pexels 补充。
        _kb_duration = 0.0
        for _p in result:
            _ext = os.path.splitext(_p)[1].lower()
            if _ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                try:
                    _clip = VideoFileClip(_p)
                    _kb_duration += min(_clip.duration, max_clip_duration)
                    _clip.close()
                except Exception:
                    _kb_duration += max_clip_duration
            else:
                _kb_duration += max_clip_duration

        _remaining = audio_duration - _kb_duration
        logger.info(
            f"kb materials: {len(result)} files, {_kb_duration:.1f}s covered, "
            f"{max(0, _remaining):.1f}s remaining"
        )

        if _remaining > 0:
            logger.info(
                f"kb materials cover {_kb_duration:.1f}s, "
                f"need {_remaining:.1f}s more. "
                f"Video assembly will loop existing {len(result)} materials."
            )

        return result

    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay
    elif source == "coverr":
        search_videos = search_videos_coverr

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    if match_script_order:
        return _download_videos_by_script_order(
            task_id=task_id,
            search_terms=search_terms,
            search_videos=search_videos,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
            # 分镜模式：每个关键词只取第一个素材，保证一一对应
            one_per_term=True,
        )

    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    for item in valid_video_items:
        try:
            logger.info(f"downloading video: {item.url}")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
    return video_paths


def _download_videos_by_script_order(
    task_id: str,
    search_terms: List[str],
    search_videos,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
    one_per_term: bool = False,
) -> List[str]:
    """
    按脚本文案顺序下载素材。

    默认下载逻辑会把所有关键词的候选素材合并成一个大列表；如果第一个
    关键词返回很多结果，最终下载时可能一直消耗这个关键词的素材，后续
    脚本主题就排不上时间线。这里按关键词分组后轮询下载：
    第 1 轮取每个关键词的第 1 个候选，第 2 轮取每个关键词的第 2 个候选。
    这样在不重写视频合成引擎的前提下，尽量保证素材顺序贴近文案顺序。
    """
    logger.info("downloading videos with script-order material matching")
    candidate_groups = []
    valid_video_urls = set()
    found_duration = 0.0

    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        term_items = []
        for item in video_items:
            if item.url in valid_video_urls:
                continue
            term_items.append(item)
            valid_video_urls.add(item.url)
            found_duration += item.duration

        if term_items:
            candidate_groups.append((search_term, term_items))

    logger.info(
        f"found total ordered video candidates: {sum(len(items) for _, items in candidate_groups)}, "
        f"required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )

    video_paths = []
    total_duration = 0.0
    candidate_index = 0
    # 分镜模式：每个关键词只取第一个素材，跳过轮询循环
    _max_index = 0 if one_per_term else 999
    while candidate_groups and total_duration <= audio_duration:
        if one_per_term and candidate_index > _max_index:
            break
        has_candidate = False
        for search_term, term_items in candidate_groups:
            if candidate_index >= len(term_items):
                continue

            has_candidate = True
            item = term_items[candidate_index]
            try:
                logger.info(
                    f"downloading ordered video for '{search_term}': {item.url}"
                )
                saved_video_path = save_video(
                    video_url=item.url, save_dir=material_directory
                )
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    total_duration += min(max_clip_duration, item.duration)
                    if total_duration > audio_duration:
                        logger.info(
                            f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                        )
                        break
            except Exception as e:
                logger.error(
                    f"failed to download ordered video: {utils.to_json(item)} => {str(e)}"
                )

        if not has_candidate:
            break
        candidate_index += 1

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    return video_paths


def download_videos_by_storyboard(
    task_id: str,
    storyboard: list,
    video_subject: str,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    clip_durations: list = None,
    kb_category: str = "",
) -> list:
    """Per-shot material download with sentence-level semantic matching.

    Each storyboard shot searches for matching KB media:
      1. Sentence semantic — shot.text (description-aware, via relevant_media)
      2. Subject fallback   — video_subject
      3. Neighbor reuse     — borrow from nearest matched scene

    A relevance threshold guards against off-topic drift: shots below it
    fall back to subject, then reuse, instead of forcing an irrelevant
    material.

    Returns a list of material paths in storyboard shot order.
    Empty slots (all layers exhausted) are filled via neighbor reuse.
    Returns [] only when EVERY scene fails (KB unreachable or empty).

    """
    material_directory = utils.task_dir(task_id)
    os.makedirs(material_directory, exist_ok=True)

    if not storyboard:
        logger.warning("empty storyboard, falling back to flat download")
        return []

    if not kb_client.is_healthy():
        logger.warning("KB unreachable for per-scene material search")
        return []

    n_shots = len(storyboard)
    seen_names = set()
    scene_materials = [""] * n_shots

    # Normalize per-shot durations: prefer clip_durations, else uniform
    if clip_durations:
        _durations = list(clip_durations)
        while len(_durations) < n_shots:
            _durations.append(max_clip_duration)
        _durations = _durations[:n_shots]
    else:
        _durations = [max_clip_duration] * n_shots

    logger.info(
        f"per-scene KB download: {n_shots} shots, "
        f"durations={[f'{d:.1f}s' for d in _durations[:5]]}"
        f"{'...' if len(_durations) > 5 else ''}"
    )

    # 句级语义阈值：低于此分数视为"无好素材"，回退主题词兜底/复用。
    _MIN_SEMANTIC_SCORE = 0.3
    _low_coverage_shots = 0

    for i, shot in enumerate(storyboard):
        shot_text = str(shot.get("text", "")).strip()
        keywords_cn = shot.get("keywords_cn", []) or []

        # ---- Layer 1: 句级语义匹配 ----
        # 用每句原文语义直接匹配素材的视觉描述，实现文案↔素材句级对应。
        # 不再把 video_subject 前置，避免主题词淹没分镜自身的语义。
        _queries = [shot_text] if shot_text else []
        for kw in keywords_cn:
            if kw and kw not in _queries:
                _queries.append(kw)

        for qtext in _queries:
            results = kb_client.relevant_media(
                qtext, top_k=8, category=kb_category
            )
            for item in results:
                name = item.get("name", "")
                if name in seen_names:
                    continue
                if float(item.get("score", 0.0)) < _MIN_SEMANTIC_SCORE:
                    break  # 结果按分数降序，低于阈值则后续更差
                seen_names.add(name)
                local = kb_client.download_media(name, material_directory)
                if local:
                    scene_materials[i] = local
                    logger.info(
                        f"shot {i+1}/{n_shots} [{_durations[i]:.1f}s] "
                        f"KB semantic '{qtext[:40]}' -> {name}"
                    )
                    break
            if scene_materials[i]:
                break

        if scene_materials[i]:
            continue

        # ---- Layer 2: 主题词兜底 ----
        if video_subject:
            results = kb_client.relevant_media(
                video_subject, top_k=8, category=kb_category
            )
            for item in results:
                name = item.get("name", "")
                if name in seen_names:
                    continue
                if float(item.get("score", 0.0)) < _MIN_SEMANTIC_SCORE:
                    break
                seen_names.add(name)
                local = kb_client.download_media(name, material_directory)
                if local:
                    scene_materials[i] = local
                    logger.info(
                        f"shot {i+1}/{n_shots} [{_durations[i]:.1f}s] "
                        f"KB subject fallback '{video_subject[:40]}' -> {name}"
                    )
                    break

        if not scene_materials[i]:
            _low_coverage_shots += 1
            logger.warning(
                f"shot {i+1}/{n_shots}: no semantic match above threshold "
                f"({_MIN_SEMANTIC_SCORE}), will attempt neighbor reuse"
            )

    # ---- Layer 3: Neighbor reuse ----
    for i in range(n_shots):
        if scene_materials[i]:
            continue
        for offset in range(1, n_shots):
            src = i - offset
            if src >= 0 and scene_materials[src]:
                scene_materials[i] = scene_materials[src]
                logger.info(
                    f"shot {i+1}: reused material from shot {src+1} "
                    f"(offset={offset})"
                )
                break
            src = i + offset
            if src < n_shots and scene_materials[src]:
                scene_materials[i] = scene_materials[src]
                logger.info(
                    f"shot {i+1}: reused material from shot {src+1} "
                    f"(offset={offset})"
                )
                break

    if _low_coverage_shots > 0:
        logger.warning(
            f"per-scene KB: {_low_coverage_shots}/{n_shots} shots fell back to "
            f"reuse — subject may have low KB material coverage"
        )

    # Stats
    found = sum(1 for m in scene_materials if m)
    unique = len(set(m for m in scene_materials if m))
    missing = n_shots - found

    if found == 0:
        logger.error(
            f"per-scene KB: ALL {n_shots} scenes failed to find materials"
        )
        return []

    logger.success(
        f"per-scene KB: {found}/{n_shots} scenes have materials "
        f"(unique sources: {unique}, missing after reuse: {missing})"
    )

    return scene_materials


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )


def download_media_by_filenames(
    task_id: str,
    filenames: list,
    kb_category: str = "",
) -> list:
    """Download KB media directly by filename (no keyword search).

    For use with generate_script_with_storyboard which assigns specific media
    (image or video) to each paragraph. Downloads each filename from the KB
    server.

    Returns a list of local file paths in the same order as filenames.
    Empty strings for failed or empty filenames.
    """
    material_directory = utils.task_dir(task_id)
    os.makedirs(material_directory, exist_ok=True)

    if not kb_client.is_healthy():
        logger.warning("KB unreachable for direct media download")
        return []

    if not filenames:
        logger.warning("no filenames provided for direct download")
        return []

    materials = []
    for i, fname in enumerate(filenames):
        if not fname or not str(fname).strip():
            materials.append("")
            logger.warning(f"shot {i+1}: empty filename, skipping")
            continue
        fname = str(fname).strip()
        local = kb_client.download_media(fname, material_directory)
        if local:
            materials.append(local)
            logger.info(
                f"shot {i+1}: downloaded KB media '{fname}' -> "
                f"{os.path.basename(local)}"
            )
        else:
            materials.append("")
            logger.warning(
                f"shot {i+1}: failed to download KB media '{fname}'"
            )

    found = sum(1 for m in materials if m)
    unique = len(set(m for m in materials if m))
    logger.success(
        f"direct KB media download: {found}/{len(filenames)} succeeded "
        f"(unique sources: {unique})"
    )
    return materials


def download_images_by_filenames(
    task_id: str,
    filenames: list,
    kb_category: str = "",
) -> list:
    """Backward-compat alias for download_media_by_filenames."""
    return download_media_by_filenames(task_id, filenames, kb_category)

