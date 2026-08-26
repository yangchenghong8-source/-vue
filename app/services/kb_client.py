"""Knowledge Base client — 封装对 kb-app 的 HTTP 调用。

用法:
    from app.services.kb_client import kb_client

    # 语义检索
    results = kb_client.search_knowledge("净水设备", top_k=5)

    # 搜索媒体文件
    media = kb_client.search_media("净水设备", top_k=10)

    # 获取知识库文档列表
    docs = kb_client.list_documents()

    # 下载媒体文件到本地
    local_path = kb_client.download_media("xxx.jpg", save_dir="/tmp")

环境变量:
    KB_API_BASE   — KB 服务地址，默认 http://127.0.0.1:3001
    KB_API_TOKEN  — 认证 token（优先级最高）
    KB_API_USER   — 登录用户名（token 未设置时自动登录）
    KB_API_PASS   — 登录密码
"""
from __future__ import annotations

import os
import time
from typing import List, Optional

import requests
from loguru import logger

# 媒体扩展名（与 kb-app 的 MEDIA_IMAGE_EXTS / MEDIA_VIDEO_EXTS 保持一致）
KB_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
KB_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".webm"}


def is_text_document(filename: str) -> bool:
    """判断是否为文字/办公文档（排除图片/视频）。

    图片/视频也会被 kb-app 向量化并混入 documents 列表，生成口播脚本时应
    只保留文字文档，用此函数过滤。
    """
    ext = os.path.splitext(filename or "")[-1].lower()
    return ext not in KB_IMAGE_EXTS and ext not in KB_VIDEO_EXTS


# kb-app 默认地址（同机部署）
_KB_API_BASE = os.getenv("KB_API_BASE", "http://127.0.0.1:3001")
_KB_TIMEOUT = (5, 30)  # (connect, read)
_KB_MAX_RETRIES = 2

# 认证配置
_KB_API_TOKEN = os.getenv("KB_API_TOKEN", "")
_KB_API_USER = os.getenv("KB_API_USER", "")
_KB_API_PASS = os.getenv("KB_API_PASS", "")


class KbClient:
    """知识库客户端，所有方法失败时返回空结果/None，不抛异常。"""

    def __init__(self, base_url: str = _KB_API_BASE):
        self.base_url = base_url.rstrip("/")
        self._healthy = True
        self._last_check = 0.0
        self._token = _KB_API_TOKEN
        self._token_expiry = 0.0

    # ── 认证 ────────────────────────────────────────────

    def _ensure_token(self):
        """确保有有效 token。优先级：环境变量 > 自动登录。"""
        if self._token and time.time() < self._token_expiry:
            return

        # 如果环境变量提供了 token，直接使用
        if _KB_API_TOKEN:
            self._token = _KB_API_TOKEN
            self._token_expiry = float("inf")
            return

        # 自动登录
        if _KB_API_USER and _KB_API_PASS:
            try:
                r = requests.post(
                    f"{self.base_url}/api/auth/login",
                    json={"username": _KB_API_USER, "password": _KB_API_PASS},
                    timeout=_KB_TIMEOUT,
                )
                if r.status_code == 200:
                    data = r.json()
                    self._token = data.get("token", "")
                    # 默认 24 小时有效
                    self._token_expiry = time.time() + 86400
                    logger.info("kb-app: auto-login successful")
                else:
                    logger.warning(f"kb-app login failed: {r.status_code}")
            except Exception as e:
                logger.warning(f"kb-app login error: {e}")

    def _headers(self) -> dict:
        """构建请求头，包含认证信息。"""
        self._ensure_token()
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # ── 内部 ────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict | list | None:
        """GET 请求，失败返回 None。"""
        url = f"{self.base_url}{path}"
        for attempt in range(_KB_MAX_RETRIES + 1):
            try:
                r = requests.get(
                    url, params=params, timeout=_KB_TIMEOUT,
                    headers=self._headers(),
                )
                if r.status_code == 200:
                    self._healthy = True
                    self._last_check = time.time()
                    return r.json()
                if r.status_code == 401:
                    # Token 过期，清除后重试一次
                    self._token = ""
                    self._token_expiry = 0
                    logger.warning(f"kb-app 401, re-authenticating...")
                    continue
                if r.status_code == 404:
                    return None
                logger.warning(f"kb-app {url} returned {r.status_code}")
            except requests.exceptions.Timeout:
                logger.warning(f"kb-app timeout: {url} (attempt {attempt+1})")
            except requests.exceptions.ConnectionError:
                logger.warning(f"kb-app unreachable: {url}")
                self._healthy = False
                break
            except Exception as e:
                logger.warning(f"kb-app request failed: {url} - {e}")
        return None

    def _download(self, path: str, save_path: str, params: dict = None) -> Optional[str]:
        """下载文件到本地，成功返回路径，失败返回 None。

        网络类瞬时故障（连接中断 / 超时 / 5xx / 空文件）会重试，避免单个素材
        下载抖动拖垮整条任务；确定性错误（404 / 其它异常）不重试，立即返回。
        """
        url = f"{self.base_url}{path}"
        for attempt in range(_KB_MAX_RETRIES + 1):
            try:
                r = requests.get(
                    url, params=params, timeout=(10, 120), stream=True,
                    headers=self._headers(),
                )
                if r.status_code == 200:
                    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                    with open(save_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                    if os.path.getsize(save_path) > 0:
                        logger.info(f"kb-app downloaded: {save_path}")
                        return save_path
                    os.remove(save_path)
                    logger.warning(
                        f"kb-app download empty: {url} (attempt {attempt+1})"
                    )
                elif r.status_code == 404:
                    logger.warning(f"kb-app download 404: {url}")
                    return None
                else:
                    logger.warning(
                        f"kb-app download {url} returned {r.status_code} "
                        f"(attempt {attempt+1})"
                    )
            except requests.exceptions.Timeout:
                logger.warning(f"kb-app download timeout: {url} (attempt {attempt+1})")
            except requests.exceptions.ConnectionError:
                logger.warning(
                    f"kb-app download connection error: {url} (attempt {attempt+1})"
                )
            except Exception as e:
                logger.warning(f"kb-app download failed: {url} - {e}")
                return None
        return None

    # ── 公开 API ─────────────────────────────────────────

    def is_healthy(self) -> bool:
        """检查 kb-app 是否可达（带 30s 缓存）。"""
        if time.time() - self._last_check < 30:
            return self._healthy
        result = self._get("/api/health")
        self._healthy = result is not None
        self._last_check = time.time()
        return self._healthy

    def search_knowledge(self, query: str, top_k: int = 5) -> list[dict]:
        """语义检索知识库，返回 [{content, metadata, score}, ...]."""
        result = self._get("/api/knowledge/search", {"q": query, "top_k": top_k})
        if isinstance(result, list):
            return result
        return []

    def chat(self, query: str, use_knowledge: bool = True) -> Optional[str]:
        """通过 kb-app RAG 对话获取知识内容（非流式）。"""
        body = {
            "messages": [{"role": "user", "content": query}],
            "use_knowledge": use_knowledge,
            "temperature": 0.3,
        }
        url = f"{self.base_url}/api/chat/non-stream"
        try:
            r = requests.post(
                url, json=body, timeout=(10, 60),
                headers=self._headers(),
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("content", "")
        except Exception as e:
            logger.warning(f"kb-app chat failed: {e}")
        return None

    def list_categories(self) -> list:
        """Get KB category list."""
        result = self._get("/api/knowledge/categories")
        if isinstance(result, dict):
            return result.get("categories", [])
        return []

    def search_media(
        self, query: str, top_k: int = 10, semantic: bool = True,
        file_type: str = "all",
        category: str = "",
    ) -> list[dict]:
        """搜索知识库中的媒体文件，返回 [{name, path, score}, ...].

        file_type: "video" | "image" | "all" — 按类型过滤媒体文件。
        """
        params = {
            "q": query, "top_k": top_k, "semantic": str(semantic).lower(),
            "file_type": file_type,
            "category": category,
        }
        result = self._get("/api/knowledge/media", params)
        if isinstance(result, list):
            return result
        return []

    def download_media(self, filename: str, save_dir: str) -> Optional[str]:
        """从 kb-app 下载媒体文件到本地目录。"""
        save_path = os.path.join(save_dir, filename)
        # 如果已存在且非空，跳过
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            return save_path
        return self._download("/api/knowledge/media/file", save_path, {"name": filename})

    def list_documents(self, search: str = "", category: str = "") -> list[dict]:
        """获取知识库文档列表。"""
        params = {}
        if search:
            params["search"] = search
        if category:
            params["category"] = category
        result = self._get("/api/knowledge/documents", params)
        if isinstance(result, dict):
            return result.get("documents", [])
        return []

    def list_media(self, file_type: str = "", category: str = "") -> list[dict]:
        """列出所有媒体文件。file_type: image / video / all；category: 按目录分类过滤。"""
        params = {}
        if file_type:
            params["file_type"] = file_type
        if category:
            params["category"] = category
        result = self._get("/api/knowledge/media/list", params)
        if isinstance(result, dict):
            return result.get("files", [])
        return []

    def list_media_category_tree(self, file_type: str = "all") -> list[dict]:
        """列出知识库分类树（一级/二级/三级层级），返回 [{name, full, count, prefixes, children}]。

        file_type: all / image / video（媒体素材）或 doc（文字文档）。
        """
        result = self._get("/api/knowledge/media/categories", {"file_type": file_type})
        if isinstance(result, dict):
            return result.get("tree", [])
        return []

    def list_media_sampled(self, category: str, limit: int = 40) -> list[dict]:
        """按分类取素材并抽样（优先有视觉描述的素材），用于素材驱动脚本生成。"""
        media = self.list_media(category=category)
        described = [m for m in media if (m.get("description") or "").strip()]
        rest = [m for m in media if not (m.get("description") or "").strip()]
        return (described + rest)[:limit]

    def relevant_media(self, query: str, top_k: int = 40, category: str = "") -> list[dict]:
        """按主题检索相关媒体文件（图片+视频，含分类目录匹配）。

        category: 知识库分类 full 路径前缀（逗号分隔多前缀），做系列硬约束过滤。
        """
        params = {"q": query, "top_k": top_k}
        if category:
            params["category"] = category
        result = self._get("/api/knowledge/media/relevant", params)
        if isinstance(result, dict):
            return result.get("files", [])
        return []

    def precheck_series(self, category: str = "", min_assets: int = 6) -> dict:
        """系列素材预检：返回 {category, total, images, videos, min_assets, sufficient}。

        生成前调用，判断所选系列素材是否足够；不足时调用方应中止并提示，
        而不是静默漂移到其他系列。
        """
        result = self._get(
            "/api/knowledge/media/precheck",
            {"category": category, "min_assets": min_assets},
        )
        if isinstance(result, dict):
            return result
        # kb-app 不可达或接口异常时，保守返回 insufficient=False 阻止生成
        return {
            "category": category,
            "total": 0,
            "images": 0,
            "videos": 0,
            "min_assets": min_assets,
            "sufficient": False,
            "error": "kb-app precheck unavailable",
        }


# 全局单例
kb_client = KbClient()
