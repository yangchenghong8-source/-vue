"""Kimi 视觉模型客户端 — 分析知识库图片，生成宣传片运镜提示词。

用途：
    从知识库系列里取出的素材图，先交给视觉模型判断"这张图适合拍成什么
    画面 / 用什么运镜"，为即梦图生视频提供分镜提示词。本模块只负责"看图
    说话"，不关心素材从哪来、视频往哪去。

配置（config.toml）：
    moonshot_api_key  — 复用现有 moonshot provider 的 Kimi key（一个 key 通吃
                        文本模型与视觉模型，无需单独申请）
    moonshot_vl_model — 视觉模型名，默认 "kimi-k3"（Kimi3 视觉模型，已实测支持
                        base64 图片理解）
    moonshot_vl_base_url — 默认走 Kimi 国内 OpenAI 兼容端点

设计约定：
    key 缺失时通过 is_enabled() 暴露状态，analyze_image() 抛出带明确提示的
    异常，由上层（task 编排）转成结构化失败，绝不静默吞掉或崩溃任务。

Kimi 视觉接口约束：
    - 仅接受 base64 data URL 图片（不接受公网 URL），本模块已用 base64 编码。
    - message.content 必须是多 part 数组，本模块已按此构造。
    - 不传 temperature（Kimi 视觉模型 temperature 固定，传值会返回 HTTP 400）。
"""
from __future__ import annotations

import base64
import mimetypes
import os
from typing import Optional

from loguru import logger
from openai import OpenAI

from app.config import config

# Kimi 国内 OpenAI 兼容端点（视觉模型与文本模型同一端点）
_DEFAULT_VL_BASE_URL = "https://api.moonshot.cn/v1"
_DEFAULT_VL_MODEL = "kimi-k3"
_MAX_RETRIES = 3


def _image_to_data_url(image_path: str) -> str:
    """把本地图片转成 data URL（base64），供视觉模型 image_url 输入。"""
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"image not found: {image_path}")
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    with open(image_path, "rb") as f:
        raw = f.read()
    if not raw:
        raise ValueError(f"empty image file: {image_path}")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class VisionClient:
    """Kimi 视觉客户端，所有网络失败带重试，最终抛出明确异常。"""

    def __init__(self):
        self.api_key = config.app.get("moonshot_api_key", "")
        self.model = config.app.get("moonshot_vl_model", "") or _DEFAULT_VL_MODEL
        self.base_url = (
            config.app.get("moonshot_vl_base_url", "") or _DEFAULT_VL_BASE_URL
        )
        self._client: Optional[OpenAI] = None

    def is_enabled(self) -> bool:
        """key 是否已配置。未配置时上层应阻止进入视觉分析链路。"""
        return bool(self.api_key)

    def _get_client(self) -> OpenAI:
        if not self.api_key:
            raise ValueError(
                "vision: moonshot_api_key is not set, please set it in config.toml "
                "(视觉模型复用 Kimi/Moonshot 的同一个 key)"
            )
        if self._client is None:
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=60.0,
            )
        return self._client

    def analyze_image(self, image_path: str, prompt: str) -> str:
        """分析单张图片，返回模型输出的文本（运镜提示词 / 画面描述）。

        Args:
            image_path: 本地图片绝对路径。
            prompt: 分析指令，例如"描述这张图并给出 3-5 秒宣传片运镜提示词"。

        Raises:
            ValueError: key 缺失 / 图片不可读 / 模型返回空。
            Exception: 网络失败重试耗尽后原样上抛。
        """
        client = self._get_client()
        data_url = _image_to_data_url(image_path)

        last_err: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": data_url}},
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                )
                text = (response.choices[0].message.content or "").strip()
                if not text:
                    raise ValueError("[vision] returned empty content")
                return text
            except ValueError:
                # 确定性错误（空内容 / 结构异常）不重试，直接上抛
                raise
            except Exception as e:  # 网络 / 超时类瞬时故障
                last_err = e
                logger.warning(
                    f"vision analyze_image failed (attempt {attempt + 1}/{_MAX_RETRIES}): {e}"
                )
        raise RuntimeError(
            f"[vision] analyze_image failed after {_MAX_RETRIES} attempts: {last_err}"
        )


# 全局单例
vision_client = VisionClient()
