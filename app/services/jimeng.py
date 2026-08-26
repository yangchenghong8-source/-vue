"""即梦 / 火山方舟 图生视频客户端 — 图片 + 运镜提示词 → 视频片段。

用途：
    宣传片分镜脚本生成后，每个镜头（一张 KB 素材图 + 一条运镜提示词）交给
    即梦图生视频，产出一段 3-5 秒的视频片段；上层再用 ffmpeg 按顺序拼接。

配置（config.toml）：
    volcengine_api_key — 复用现有 volcengine provider 的火山方舟 key（一个 key
                         通吃豆包 LLM 与即梦视频生成，无需单独申请）
    jimeng_model       — 视频生成模型名，默认 "doubao-seedance-2-0-mini-260615"
                         （Seedance 2.0 mini 高性价比版，约 0.5 元/秒；账号需先在
                         方舟控制台开通。要更高画质可改 doubao-seedance-2-5-260628）
    jimeng_base_url    — 默认走火山方舟 ark 端点

接口（火山方舟 视频生成，异步任务式）：
    提交  POST /api/v3/contents/generations/tasks
          body: {model, content: [{image_url}, {text: "运镜 --duration 5 --ratio 16:9"}]}
    查询  GET  /api/v3/contents/generations/tasks/{id}
          状态: queued / running / succeeded / failed / cancelled / expired
    成功  content.video_url 为 MP4 下载地址（24h 有效，需及时下载）

设计约定：
    key 后续由用户补充。is_enabled() 暴露状态，image_to_video() 在 key 缺失或
    生成失败时抛出明确异常，由上层转成结构化失败，绝不静默吞掉或崩溃任务。
"""
from __future__ import annotations

import base64
import mimetypes
import os
import time
from typing import Optional

import requests
from loguru import logger

from app.config import config

_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_DEFAULT_MODEL = "doubao-seedance-2-0-mini-260615"
_TASK_TIMEOUT = (10, 60)          # (connect, read) 提交/查询请求
_DOWNLOAD_TIMEOUT = (10, 120)     # 视频下载
_POLL_INTERVAL = 10               # 轮询间隔（秒），官方建议 8-15s
_MAX_POLL_ATTEMPTS = 60           # 最多轮询 60 次（约 10 分钟）
_MAX_IMAGE_EDGE = 6000            # 火山方舟视频生成图片单边像素上限，超限需等比缩放
_MIN_ASPECT_RATIO = 0.40          # 方舟图生视频宽高比下限（宽/高），超限需居中裁剪
_MAX_ASPECT_RATIO = 2.50          # 方舟图生视频宽高比上限（宽/高），超限需居中裁剪
_SUCCEEDED = "succeeded"
_FAILED = "failed"
_CANCELLED = "cancelled"
_EXPIRED = "expired"


def _prepare_image_bytes(image_path: str) -> tuple[bytes, str]:
    """读取图片字节，超方舟限制时预处理，返回 (字节, mime)。

    方舟图生视频对输入图有两项约束，超限会被 InvalidParameter 拒绝：
        1. 单边 ≤ 6000px（KB 高清竖图如 3628×6047 会触发）；
        2. 宽高比 ∈ [0.40, 2.50]（超宽 banner 图如 7.74 会触发）。
    这里统一在编码前做等比缩放 + 居中裁剪，保证每个镜头都能生成。
    无需预处理或 Pillow 不可用时回退原始字节，不阻断主流程。
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"image not found: {image_path}")
    with open(image_path, "rb") as f:
        raw = f.read()
    if not raw:
        raise ValueError(f"empty image file: {image_path}")

    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        orig_w, orig_h = img.size
        w, h = orig_w, orig_h
        changed = False

        # 1. 单边超限 → 等比缩放
        if max(w, h) > _MAX_IMAGE_EDGE:
            scale = _MAX_IMAGE_EDGE / max(w, h)
            w, h = max(1, int(w * scale)), max(1, int(h * scale))
            img = img.resize((w, h), Image.LANCZOS)
            changed = True

        # 2. 宽高比超限 → 居中裁剪（裁剪不改变有效内容比例，避免拉伸变形）
        aspect = w / h
        if aspect > _MAX_ASPECT_RATIO:
            new_w = max(1, int(h * _MAX_ASPECT_RATIO))
            left = (w - new_w) // 2
            img = img.crop((left, 0, left + new_w, h))
            w, h = new_w, h
            changed = True
        elif aspect < _MIN_ASPECT_RATIO:
            new_h = max(1, int(w / _MIN_ASPECT_RATIO))
            top = (h - new_h) // 2
            img = img.crop((0, top, w, top + new_h))
            w, h = w, new_h
            changed = True

        if not changed:
            return raw, mime

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        logger.info(
            f"jimeng: image {os.path.basename(image_path)} adjusted "
            f"{orig_w}x{orig_h} -> {w}x{h}"
        )
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        logger.warning(f"jimeng: image preprocess skipped for '{image_path}': {e}")
        return raw, mime


def _image_to_data_url(image_path: str) -> str:
    data, mime = _prepare_image_bytes(image_path)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class JimengClient:
    """即梦图生视频客户端。"""

    def __init__(self):
        self.api_key = config.app.get("volcengine_api_key", "")
        self.model = config.app.get("jimeng_model", "") or _DEFAULT_MODEL
        self.base_url = (
            config.app.get("jimeng_base_url", "") or _DEFAULT_BASE_URL
        ).rstrip("/")

    def is_enabled(self) -> bool:
        """key 是否已配置。未配置时上层应阻止进入即梦生成链路。"""
        return bool(self.api_key)

    def _headers(self) -> dict:
        if not self.api_key:
            raise ValueError(
                "jimeng: volcengine_api_key is not set, please set it in "
                "config.toml (即梦视频生成复用火山方舟的同一个 key)"
            )
        return {"Authorization": f"Bearer {self.api_key}"}

    def _submit_task(self, image_data_url: str, prompt: str, duration: int, ratio: str) -> str:
        """提交图生视频任务，返回 task_id。"""
        body = {
            "model": self.model,
            "content": [
                {"type": "image_url", "image_url": {"url": image_data_url}},
                {
                    "type": "text",
                    "text": f"{prompt} --duration {duration} --ratio {ratio}",
                },
            ],
        }
        url = f"{self.base_url}/contents/generations/tasks"
        r = requests.post(url, json=body, headers=self._headers(), timeout=_TASK_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(
                f"jimeng submit failed: HTTP {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        task_id = data.get("id") or data.get("task_id")
        if not task_id:
            raise RuntimeError(f"jimeng submit returned no task id: {data}")
        return task_id

    def _poll_task(self, task_id: str) -> dict:
        """轮询任务直到终态，返回终态响应体。"""
        url = f"{self.base_url}/contents/generations/tasks/{task_id}"
        for _ in range(_MAX_POLL_ATTEMPTS):
            r = requests.get(url, headers=self._headers(), timeout=_TASK_TIMEOUT)
            if r.status_code != 200:
                # 查询失败通常是瞬时抖动，继续轮询而不是立刻放弃
                logger.warning(f"jimeng poll HTTP {r.status_code}, will retry")
                time.sleep(_POLL_INTERVAL)
                continue
            data = r.json()
            status = data.get("status", "")
            if status == _SUCCEEDED:
                return data
            if status in (_FAILED, _CANCELLED, _EXPIRED):
                raise RuntimeError(
                    f"jimeng task {task_id} ended with status '{status}': {data}"
                )
            # queued / running，继续等待
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(
            f"jimeng task {task_id} not finished within "
            f"{_MAX_POLL_ATTEMPTS * _POLL_INTERVAL}s"
        )

    def _download_video(self, video_url: str, output_path: str) -> str:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        r = requests.get(video_url, timeout=_DOWNLOAD_TIMEOUT, stream=True)
        if r.status_code != 200:
            raise RuntimeError(f"jimeng video download HTTP {r.status_code}")
        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        if os.path.getsize(output_path) == 0:
            os.remove(output_path)
            raise RuntimeError("jimeng video download empty")
        logger.info(f"jimeng video saved: {output_path}")
        return output_path

    def image_to_video(
        self,
        image_path: str,
        prompt: str,
        output_path: str,
        duration: int = 5,
        ratio: str = "16:9",
    ) -> str:
        """图生视频：提交任务 → 轮询 → 下载，成功返回视频本地路径。

        Args:
            image_path: 首帧图片（本地路径）。
            prompt: 运镜 / 画面描述提示词。
            output_path: 视频输出绝对路径（.mp4）。
            duration: 生成时长（秒），即梦标准为 5 / 10。
            ratio: 宽高比，如 "16:9" / "9:16" / "1:1"。

        Raises:
            ValueError: key 缺失 / 图片不可读。
            RuntimeError / TimeoutError: 生成失败，上层转成结构化失败。
        """
        data_url = _image_to_data_url(image_path)
        task_id = self._submit_task(data_url, prompt, duration, ratio)
        logger.info(f"jimeng task submitted: {task_id}, duration={duration}s ratio={ratio}")
        result = self._poll_task(task_id)
        content = result.get("content") or {}
        video_url = content.get("video_url", "")
        if not video_url:
            raise RuntimeError(f"jimeng task {task_id} succeeded but no video_url")
        return self._download_video(video_url, output_path)


# 全局单例
jimeng_client = JimengClient()
