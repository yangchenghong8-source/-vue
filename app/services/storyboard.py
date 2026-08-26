"""宣传片分镜脚本生成编排 — 视觉模型分析 KB 图片 → 组装分镜（图 + 客观视觉描述 + 时长）。

职责边界：
    - 只负责"取图 → 看图写客观视觉描述 → 组装分镜"，产出一份即梦可直接消费的分镜表。
    - 运镜 / 画面运动 / 旁白文案由 DeepSeek 在后续分镜创作阶段自主构建，本模块不预分配运镜。
    - 配音 / 字幕 / BGM 由平台后续流程处理，本模块不涉及。

多样性保证：
    1. 图片选择多样性 —— 从系列里尽量挑不同的图，优先带视觉描述的素材。
    2. 顺序多样性     —— 图片顺序打乱，每次生成可有差异。
    运镜方式的多样性由 DeepSeek 在分镜创作阶段自主控制。

时长约束：
    6-8 个镜头，每段目标 3-5 秒，总时长 ≤ 30 秒。即梦单段生成时长固定为
    SHOT_DURATION 秒（标准 5s），超出目标时由上层 ffmpeg 拼接阶段裁剪。
"""
from __future__ import annotations

import os
import random
from typing import Optional

from loguru import logger

from app.services.kb_client import kb_client
from app.services.vision import vision_client

# 即梦图生视频仅支持静态图片扩展名（.gif 等动态图不支持，选图阶段即过滤）
_SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

# 即梦单段生成时长（秒），标准为 5s 或 10s；这里取 5s 以贴合 ≤30s 约束
SHOT_DURATION = 5
# 镜头数范围
MIN_SHOTS = 6
MAX_SHOTS = 8
# 单段目标时长范围（秒），用于 ffmpeg 裁剪，非即梦生成时长
MIN_CLIP_DURATION = 3.0
MAX_CLIP_DURATION = 5.0
# 全片时长上限（秒）
MAX_TOTAL_DURATION = 30.0

_ANALYZE_PROMPT_TEMPLATE = (
    "请客观描述这张产品/设备图片的内容，不要做任何创作。要求：\n"
    "1. 说明图片主体是什么、有哪些关键部件与细节。\n"
    "2. 描述部件之间的空间关系、材质与整体风格。\n"
    "3. 只输出客观的视觉描述，不要写运镜、不要写宣传文案、不要给视频建议。\n"
    "4. 直接输出中文描述正文，不要标题、不要编号、不要解释，60 字以内。"
)


def _pick_shots(total_assets: int, min_shots: int = MIN_SHOTS, max_shots: int = MAX_SHOTS) -> int:
    """根据素材数量决定镜头数：素材越少镜头越少，但至少 min_shots、至多 max_shots。

    6 镜头 × 5s = 30s 正好卡上限；素材充足时最多 8 镜头。
    """
    shots = min(total_assets, max_shots)
    return max(min_shots, min(shots, max_shots))


def sample_images(category: str, shot_count: int) -> list[dict]:
    """从知识库系列里多样性地挑选图片素材。

    返回 [{name, description, local_path}, ...]；图片不足时返回可取得的最大数量。
    """
    media = kb_client.list_media(file_type="image", category=category)
    if not media:
        logger.warning(f"promo storyboard: no images in category '{category}'")
        return []

    # 过滤即梦不支持的动态图（如 .gif），只保留静态图片扩展名
    media = [m for m in media if (m.get("name") or "").lower().endswith(_SUPPORTED_IMAGE_EXTS)]
    if not media:
        logger.warning(f"promo storyboard: no supported static images in category '{category}'")
        return []

    # 优先带视觉描述的图，其次其余图；打乱顺序以增强多样性
    described = [m for m in media if (m.get("description") or "").strip()]
    rest = [m for m in media if not (m.get("description") or "").strip()]
    random.shuffle(described)
    random.shuffle(rest)
    pool = (described + rest)[: shot_count * 3]  # 取 3 倍候选，供后续去重/兜底

    picked: list[dict] = []
    seen = set()
    for m in pool:
        name = m.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        picked.append(m)
        if len(picked) >= shot_count:
            break

    return picked


def _download_images(picked: list[dict], task_dir: str) -> list[dict]:
    """把挑选的图片下载到任务目录，补上 local_path。下载失败的图剔除。"""
    downloaded = []
    for m in picked:
        name = m.get("name", "")
        local = kb_client.download_media(name, task_dir)
        if local:
            m = dict(m)
            m["local_path"] = local
            downloaded.append(m)
        else:
            logger.warning(f"promo storyboard: download failed for '{name}', skip")
    return downloaded


def generate_promo_storyboard(
    kb_category: str,
    task_dir: str,
    shot_count: Optional[int] = None,
    video_subject: str = "",
    seed: Optional[int] = None,
) -> list[dict]:
    """生成宣传片分镜脚本。

    Returns:
        [{"index": 1, "media": "xxx.jpg", "local_path": "...",
          "visual_description": "...", "duration": 5.0, "clip_duration": 4.0}, ...]
        运镜提示词（camera_prompt）由 DeepSeek 在 llm.generate_jimeng_storyboard_and_script
        中根据视觉描述 + 主题自主构建，本函数不预分配运镜。
        失败（系列无图 / 视觉模型不可用）时返回空列表。
    """
    if not vision_client.is_enabled():
        logger.warning("promo storyboard: kimi vision not enabled (missing moonshot_api_key)")
        return []

    if seed is not None:
        random.seed(seed)

    # 1. 素材预检 + 选图
    media = kb_client.list_media(file_type="image", category=kb_category)
    if not media:
        logger.warning(f"promo storyboard: category '{kb_category}' has no images")
        return []

    count = shot_count or _pick_shots(len(media))
    picked = sample_images(kb_category, count)
    picked = _download_images(picked, task_dir)
    if not picked:
        logger.warning("promo storyboard: no downloadable images")
        return []

    # 2. 逐张视觉分析 + 组装分镜（串行：moonshot 账号并发限制为 1，并行会触发 429 限流）
    storyboard = []
    n = len(picked)
    # 每段目标时长 3-5s，总时长 ≤ 30s：先均分，再按剩余预算微调
    per_clip = min(
        MAX_CLIP_DURATION,
        max(MIN_CLIP_DURATION, MAX_TOTAL_DURATION / n),
    )

    for i, item in enumerate(picked, start=1):
        visual_description = ""
        try:
            visual_description = vision_client.analyze_image(
                item["local_path"], _ANALYZE_PROMPT_TEMPLATE
            )
        except Exception as e:
            logger.warning(f"promo storyboard: vision analysis failed for shot {i}: {e}")
            # 视觉识别失败时留空，交由 DeepSeek 依据主题自行发挥
            visual_description = ""

        storyboard.append(
            {
                "index": i,
                "media": item.get("name", ""),
                "local_path": item.get("local_path", ""),
                "visual_description": visual_description.strip(),
                "duration": float(SHOT_DURATION),
                "clip_duration": round(per_clip, 1),
            }
        )

    logger.success(
        f"promo storyboard: {len(storyboard)} shots, "
        f"total={sum(s['clip_duration'] for s in storyboard):.1f}s, "
        f"subject='{video_subject}'"
    )
    return storyboard


def reuse_promo_storyboard(reused_shots: list[dict], task_dir: str) -> list[dict]:
    """复用前端预览并编辑过的分镜：按 media 文件名重新下载图，补齐后端可用的 local_path。

    前端传来的分镜只有 media（文件名）与文本字段，不含后端可用的本地文件路径；
    这里按文件名重新下载素材，保留前端编辑过的 camera_prompt / visual_description 与时长。
    下载失败的镜头剔除（宁可少一个镜头，也不生成一张坏图）。

    Returns:
        [{"index", "media", "local_path", "camera_prompt", "visual_description",
          "duration", "clip_duration"}, ...]
    """
    shots = []
    for s in reused_shots or []:
        name = (s.get("media") or "").strip()
        if not name:
            logger.warning("promo storyboard: reuse shot missing media, skip")
            continue
        local = kb_client.download_media(name, task_dir)
        if not local:
            logger.warning(f"promo storyboard: reuse download failed for '{name}', skip")
            continue
        camera_prompt = (
            s.get("camera_prompt") or s.get("visual_description") or ""
        ).strip()
        shots.append(
            {
                "index": int(s.get("index", len(shots) + 1)),
                "media": name,
                "local_path": local,
                "camera_prompt": camera_prompt,
                "visual_description": (s.get("visual_description") or "").strip(),
                "duration": float(s.get("duration") or SHOT_DURATION),
                "clip_duration": float(s.get("clip_duration") or 0),
            }
        )
    if shots:
        logger.success(f"promo storyboard: reused {len(shots)} shots from frontend")
    return shots
