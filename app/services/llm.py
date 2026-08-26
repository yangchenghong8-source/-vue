import json
import logging
import math
import re
from time import perf_counter
from typing import List

from loguru import logger
from openai import AzureOpenAI, OpenAI
from openai.types.chat import ChatCompletion

from app.config import config
from app.models.llm_provider import DEFAULT_LLM_PROVIDER_ID, get_llm_provider
from app.services.kb_client import kb_client
from app.utils.utils import estimate_voiceover_duration

_max_retries = 5
MIN_SCRIPT_PARAGRAPH_NUMBER = 1
MAX_SCRIPT_PARAGRAPH_NUMBER = 10
MAX_SCRIPT_PROMPT_LENGTH = 2000
MAX_SCRIPT_SYSTEM_PROMPT_LENGTH = 8000
# 口播时长闭环：中文语速（字/秒）与目标时长容差
SCRIPT_CHARS_PER_SECOND = 4.2
SCRIPT_DURATION_TOLERANCE = 0.15
SCRIPT_DURATION_MAX_RETRIES = 2
# 素材驱动模式：每段文案的最长口播时长（秒），用于从 target_duration 推算分镜数。
# 例如 30 秒目标 → 30 // 5 = 6 段 → 6 个素材，每段不超过 5 秒。
MAX_SECONDS_PER_PARAGRAPH = 5
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_URL_USERINFO_RE = re.compile(
    r"((?:https?|wss?)://)([^/\s?#@]*:[^/\s?#@]*@)", re.IGNORECASE
)
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|key|secret|password)=)([^&#\s]+)",
    re.IGNORECASE,
)

DEFAULT_SCRIPT_SYSTEM_PROMPT = """
# Role: Video Script Generator

## Goals:
Generate a script for a video, depending on the subject of the video.

## Constrains:
1. the script is to be returned as a string with the specified number of paragraphs.
2. do not under any circumstance reference this prompt in your response.
3. get straight to the point, don't start with unnecessary things like, "welcome to this video".
4. you must not include any type of markdown or formatting in the script, never use a title.
5. only return the raw content of the script.
6. do not include "voiceover", "narrator" or similar indicators of what should be spoken at the beginning of each paragraph or line.
7. you must not mention the prompt, or anything about the script itself. also, never talk about the amount of paragraphs or lines. just write the script.
8. respond in the same language as the video subject.
""".strip()

def _normalize_text_response(content, llm_provider: str) -> str:
    # 不同 LLM SDK 在异常或被拦截场景下，可能返回 None、空字符串，
    # 甚至返回非字符串对象。这里统一做兜底校验，避免后续直接调用
    # `.replace()` 时抛出 `NoneType` 之类的属性错误。
    if content is None:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    if not isinstance(content, str):
        raise TypeError(
            f"[{llm_provider}] returned non-text content: {type(content).__name__}"
        )

    # MiniMax M3、DeepSeek R1 这类 reasoning 模型可能会把内部推理包在
    # `<think>...</think>` 中返回。视频脚本和关键词只需要最终可朗读文本，
    # 如果不在服务层统一清理，WebUI、字幕和配音都会把思考过程当正文处理。
    content = _THINK_BLOCK_RE.sub("", content)
    content = _UNCLOSED_THINK_BLOCK_RE.sub("", content).strip()
    if not content:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    return content.replace("\n", "")


def _sanitize_error_message(error: object) -> str:
    """
    清理返回给 WebUI/API 的错误信息，避免自定义 base_url 中的凭据泄露。

    一些 OpenAI-compatible SDK 会把请求 URL 原样拼进异常信息。如果用户为了
    代理网关配置了 `https://user:pass@example.com/v1`，直接返回 `str(e)`
    就会把密码暴露给页面、API 调用方或后续日志。这里仅处理错误文案，不改变
    实际请求地址，避免影响正常调用链路。
    """
    message = str(error)
    message = _URL_USERINFO_RE.sub(r"\1***:***@", message)
    message = _SENSITIVE_QUERY_RE.sub(r"\1***", message)
    return message


def _extract_chat_completion_text(response, llm_provider: str) -> str:
    # OpenAI 兼容接口在异常场景下，可能返回没有 choices、
    # 或者 choices/message/content 为空的响应对象。
    # 这里统一做结构校验，避免出现 `NoneType is not subscriptable`
    # 这类底层属性访问错误。
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError(f"[{llm_provider}] returned empty choices")

    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    if message is None:
        raise ValueError(f"[{llm_provider}] returned empty message")

    content = getattr(message, "content", None)
    return _normalize_text_response(content, llm_provider)


def _get_response_field(value, key: str):
    """兼容 dict 和 SDK 响应对象的字段读取。"""
    if isinstance(value, dict):
        return value.get(key)

    try:
        return value[key]
    except (KeyError, TypeError, AttributeError):
        return getattr(value, key, None)


def _extract_qwen_generation_text(response) -> str:
    """
    从 DashScope Generation 响应中提取文本。

    Qwen 使用 `messages` 调用时返回的是 chat 结构：
    `output.choices[0].message.content`；旧 completion 形态才会返回
    `output.text`。这里两个路径都兼容，避免 `output.text` 为 None 时
    继续 `.replace()` 触发不可诊断的 AttributeError。
    """
    output = _get_response_field(response, "output")
    choices = _get_response_field(output, "choices") if output else None
    if choices is not None:
        if not choices:
            logger.warning("Qwen returned an empty choices list")
            raise ValueError("[qwen] returned empty choices")

        first_choice = choices[0]
        message = _get_response_field(first_choice, "message")
        content = _get_response_field(message, "content") if message else None
        if content is not None:
            return _normalize_text_response(content, "qwen")

    text = _get_response_field(output, "text") if output else None
    return _normalize_text_response(text, "qwen")


def _generate_response(prompt: str) -> str:
    try:
        llm_provider = str(
            config.app.get("llm_provider", DEFAULT_LLM_PROVIDER_ID)
        ).lower()
        provider = get_llm_provider(llm_provider)
        if provider is None:
            raise ValueError(f"{llm_provider}: unsupported llm provider")

        logger.info(f"llm provider: {llm_provider}")
        api_key = config.app.get(provider.config_key("api_key"), "")
        configured_model = config.app.get(provider.config_key("model_name"), "")
        model_name = provider.resolve_model_name(configured_model)
        if configured_model and model_name != configured_model:
            logger.warning(
                f"{llm_provider} model '{configured_model}' is deprecated, "
                f"fallback to '{model_name}'"
            )
        configured_base_url = config.app.get(provider.config_key("base_url"), "")
        base_url = provider.resolve_base_url(configured_base_url)
        if configured_base_url and configured_base_url.strip().rstrip("/") in {
            url.rstrip("/") for url in provider.deprecated_base_urls
        }:
            logger.warning(
                f"{llm_provider} base URL '{configured_base_url}' is deprecated, "
                f"fallback to '{base_url}'"
            )
        adapter = provider.adapter
        api_version = ""

        # Ollama 的默认地址依赖当前是否运行在容器中，无法作为静态 Registry
        # 值保存；Registry 仍负责模型和必填规则，运行环境差异在这里解析。
        if llm_provider == "ollama":
            api_key = "ollama"
            if not base_url:
                base_url = config.get_default_ollama_base_url()

        if adapter == "azure":
            api_version = config.app.get(
                provider.config_key("api_version"), "2024-02-15-preview"
            )

        extra_values = {
            field.config_suffix: (
                config.app.get(provider.config_key(field.config_suffix), "")
                or field.default_value
            )
            for field in provider.extra_fields
        }

        if provider.requires_api_key and not api_key:
            raise ValueError(
                f"{llm_provider}: api_key is not set, please set it in the config.toml file."
            )
        if provider.requires_model_name and not model_name:
            raise ValueError(
                f"{llm_provider}: model_name is not set, please set it in the config.toml file."
            )
        if provider.requires_base_url and not base_url:
            raise ValueError(
                f"{llm_provider}: base_url is not set, please set it in the config.toml file."
            )

        for field in provider.extra_fields:
            if field.required and not extra_values[field.config_suffix]:
                raise ValueError(
                    f"{llm_provider}: {field.config_suffix} is not set, "
                    "please set it in the config.toml file."
                )

        if adapter == "qwen":
            import dashscope
            from dashscope.api_entities.dashscope_response import GenerationResponse

            dashscope.api_key = api_key
            response = dashscope.Generation.call(
                model=model_name, messages=[{"role": "user", "content": prompt}]
            )
            if response:
                if isinstance(response, GenerationResponse):
                    status_code = response.status_code
                    if status_code != 200:
                        raise Exception(
                            f'[{llm_provider}] returned an error response: "{response}"'
                        )

                    return _extract_qwen_generation_text(response)
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}"'
                    )
            else:
                raise Exception(f"[{llm_provider}] returned an empty response")

        if adapter == "gemini":
            from google import genai
            from google.genai import types

            http_options = types.HttpOptions(base_url=base_url) if base_url else None
            generation_config = types.GenerateContentConfig(
                temperature=0.5,
                top_p=1,
                top_k=1,
                max_output_tokens=2048,
                safety_settings=[
                    types.SafetySetting(
                        category="HARM_CATEGORY_HARASSMENT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_HATE_SPEECH",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_DANGEROUS_CONTENT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                ],
            )

            try:
                # 新版 google-genai 通过统一 Client 暴露模型服务。上下文管理器
                # 会在请求结束后关闭底层 HTTP 连接，避免频繁生成时积累连接资源。
                with genai.Client(
                    api_key=api_key,
                    http_options=http_options,
                ) as client:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=generation_config,
                    )
                generated_text = response.text
            except (AttributeError, IndexError, ValueError) as e:
                logger.warning(f"gemini returned invalid response content: {str(e)}")
                raise ValueError(f"[{llm_provider}] returned invalid response content")

            return _normalize_text_response(generated_text, llm_provider)

        if adapter == "cloudflare_ai_gateway":
            account_id = extra_values["account_id"]
            gateway_id = extra_values["gateway_id"]
            # Cloudflare 当前推荐的 AI Gateway REST API 兼容 OpenAI SDK。
            # Account ID 用于构造统一端点，Gateway ID 通过请求头选择；这里
            # 不再调用 Workers AI 的 /ai/run/{model} 专用接口。
            client = OpenAI(
                api_key=api_key,
                base_url=(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
                ),
                default_headers={"cf-aig-gateway-id": gateway_id},
            )
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
            )
            return _extract_chat_completion_text(response, llm_provider)

        if adapter == "litellm":
            import litellm

            if not model_name:
                raise ValueError(
                    f"{llm_provider}: model_name is not set, please set it in the config.toml file."
                )

            response = litellm.completion(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                drop_params=True,
            )

            if not response:
                raise ValueError(f"[{llm_provider}] returned empty response")
            if not getattr(response, "choices", None):
                raise ValueError(f"[{llm_provider}] returned empty response")

            return _extract_chat_completion_text(response, llm_provider)

        if adapter == "azure":
            # Azure OpenAI SDK 使用 `azure_endpoint` 和 `api_version` 生成专用请求地址，
            # 不能继续复用下面普通 OpenAI-compatible 的 `base_url` 初始化逻辑。
            # 这里在 Azure 分支内完成请求并立即返回，避免客户端被后续 fallback
            # 覆盖，导致用户配置的 Azure 凭证通过校验但实际请求没有被使用。
            logger.info(f"requesting azure chat completion, model: {model_name}")
            client = AzureOpenAI(
                api_key=api_key,
                api_version=api_version,
                azure_endpoint=base_url,
            )
            response = client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": prompt}]
            )
            if response:
                if isinstance(response, ChatCompletion):
                    return _extract_chat_completion_text(response, llm_provider)
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                        f"connection and try again."
                    )
            else:
                raise Exception(
                    f"[{llm_provider}] returned an empty response, please check your network connection and try again."
                )

        if adapter == "modelscope":
            content = ""
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
            )
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                extra_body={"enable_thinking": False},
                stream=True,
            )
            if response:
                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        content += delta.content

                if not content.strip():
                    raise ValueError("Empty content in stream response")

                return _normalize_text_response(content, llm_provider)
            else:
                raise Exception(f"[{llm_provider}] returned an empty response")

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )

        response = client.chat.completions.create(
            model=model_name, messages=[{"role": "user", "content": prompt}]
        )
        if response:
            if isinstance(response, ChatCompletion):
                return _extract_chat_completion_text(response, llm_provider)
            else:
                raise Exception(
                    f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                    f"connection and try again."
                )
        else:
            raise Exception(
                f"[{llm_provider}] returned an empty response, please check your network connection and try again."
            )

    except Exception as e:
        return f"Error: {_sanitize_error_message(e)}"


def test_connection() -> tuple[bool, str, float]:
    """
    使用当前 Provider 配置发起一次最小请求，验证实际生成链路是否可用。

    连接测试直接复用 `_generate_response()`，因此会覆盖 API Key、Base URL、
    模型名称和 Provider 专用字段，但不会进入脚本生成的重试逻辑，也不会发送
    用户的视频主题或文案。返回值依次为成功状态、错误信息和请求耗时。
    """
    started_at = perf_counter()
    response = _generate_response(prompt="Reply with exactly: OK")
    elapsed = perf_counter() - started_at

    if not response:
        error_message = "LLM returned an empty response"
        logger.warning(f"llm connection test failed: {error_message}")
        return False, error_message, elapsed

    if response.startswith("Error:"):
        error_message = response.removeprefix("Error:").strip()
        logger.warning(f"llm connection test failed: {error_message}")
        return False, error_message, elapsed

    logger.info(f"llm connection test succeeded, elapsed: {elapsed:.2f}s")
    return True, "", elapsed


def _limit_script_text(text: str | None, max_length: int, field_name: str) -> str:
    value = (text or "").strip()
    if len(value) <= max_length:
        return value

    # API 层已经用 Pydantic 做长度校验；这里继续兜底，是为了保护
    # WebUI 或内部服务直接调用 generate_script 时不会把超长提示词发送给模型，
    # 避免 token 成本异常和请求失败。
    logger.warning(
        f"{field_name} is too long and will be truncated to {max_length} characters."
    )
    return value[:max_length]


def _normalize_script_paragraph_number(paragraph_number: int | None) -> int:
    try:
        value = int(paragraph_number or MIN_SCRIPT_PARAGRAPH_NUMBER)
    except (TypeError, ValueError):
        value = MIN_SCRIPT_PARAGRAPH_NUMBER

    if value < MIN_SCRIPT_PARAGRAPH_NUMBER or value > MAX_SCRIPT_PARAGRAPH_NUMBER:
        # WebUI 和 API 都会限制范围；这里兜底处理内部调用，避免异常参数直接扩大
        # LLM 生成成本或生成空结果。
        logger.warning(
            f"script paragraph_number is out of range and will be clamped: {value}"
        )
        return max(MIN_SCRIPT_PARAGRAPH_NUMBER, min(value, MAX_SCRIPT_PARAGRAPH_NUMBER))

    return value


def _script_duration_requirement(target_duration: int) -> str:
    """把目标时长（秒）换算成 prompt 约束文案：时长 + 目标字数双写。

    双写比只写时长更能让 LLM 遵守 —— 模型对「约 126 字」的遵守度显著高于
    「约 30 秒」。字数按 SCRIPT_CHARS_PER_SECOND 换算（标准语速，不乘 voice_rate）。
    """
    if target_duration <= 0:
        return ""
    target_chars = math.ceil(target_duration * SCRIPT_CHARS_PER_SECOND)
    return (
        f"整段口播脚本的朗读时长控制在 {target_duration} 秒左右（约 {target_chars} 字），"
        "内容精炼、无冗余表述。"
    )


def _per_paragraph_duration_requirement(target_duration: int, paragraph_number: int) -> str:
    """素材驱动模式：把总时长按段落数均摊，约束每段不超过均摊时长。

    让 LLM 严格保证段数（=素材数），避免每段写长后自发减段。
    """
    if target_duration <= 0 or paragraph_number <= 0:
        return ""
    per = max(1, math.ceil(target_duration / paragraph_number))
    per_chars = math.ceil(per * SCRIPT_CHARS_PER_SECOND)
    return (
        f"每段文案不超过 {per} 秒（约 {per_chars} 字），"
        f"共 {paragraph_number} 段，逐段均匀分配总时长。"
    )


def build_script_prompt(
    video_subject: str,
    language: str = "",
    paragraph_number: int = 1,
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    knowledge_context: str = "",
    target_duration: int = 0,  # 目标口播时长（秒），0 = 不限制
) -> str:
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )
    knowledge_context = _limit_script_text(
        knowledge_context, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH // 2, "knowledge_context"
    )

    # 将"脚本生成规则"和"运行时上下文"分开拼接。这样高级用户即使覆盖默认
    # system prompt，也不会漏掉视频主题、语言、段落数这些每次生成都必须带上的参数。
    prompt = custom_system_prompt or DEFAULT_SCRIPT_SYSTEM_PROMPT

    # 注入知识库内容
    if knowledge_context:
        prompt += f"""

# Knowledge Base Context:
The following is verified information from the knowledge base. You MUST base your script on these facts. Do not fabricate any information that contradicts the knowledge below.

{knowledge_context}

# Important:
- Strictly use the facts from the knowledge base above
- If the knowledge base does not cover a specific detail, you may fill in with reasonable general knowledge
- Do not invent product names, specifications, or claims unless they appear in the knowledge base
""".rstrip()

    prompt += f"""

# Initialization:
- video subject: {video_subject}
- number of paragraphs: {paragraph_number}
""".rstrip()
    if target_duration > 0:
        prompt += f"""
- 时长要求：{_script_duration_requirement(target_duration)}
""".rstrip()
    if language:
        prompt += f"\n- language: {language}"
    if video_script_prompt:
        prompt += f"""

# Additional User Requirements:
{video_script_prompt}
""".rstrip()

    return prompt


def generate_script(
    video_subject: str,
    language: str = "",
    paragraph_number: int = 1,
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    use_knowledge: bool = False,
    kb_doc_filenames: list[str] | None = None,
    target_duration: int = 0,  # 目标口播时长（秒），0 = 不限制
) -> tuple[str, dict]:
    """Generate a video script.

    Returns:
        (script_text, kb_info) where kb_info is a dict:
        - {"used": True/False, "fallback": True/False, "chunks": int, "empty": True/False}

    当 target_duration > 0 时开启时长闭环：生成后用估算器核对，超差把实际
    字数回喂给 LLM 要求压缩/扩写，最多重试 SCRIPT_DURATION_MAX_RETRIES 次，
    最终取估算时长最接近目标的一次。约束不达标的软兜底，不截断文案。
    """
    kb_info = {"used": False, "fallback": False, "chunks": 0, "empty": False}
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )

    # ── 知识库检索 ──
    knowledge_context = ""
    if use_knowledge:
        kb_info["used"] = True
        try:
            # 构建查询：用主题 + 自定义提示词
            query = video_subject
            if video_script_prompt:
                query += " " + video_script_prompt

            if kb_client.is_healthy():
                results = kb_client.search_knowledge(query, top_k=8)
                if results:
                    kb_info["chunks"] = len(results)
                    context_parts = []
                    for i, chunk in enumerate(results, 1):
                        content = chunk.get("content", "")
                        meta = chunk.get("metadata", {})
                        fname = meta.get("filename", "unknown")
                        context_parts.append(f"[来源 {i}: {fname}]\n{content}")
                    knowledge_context = "\n\n".join(context_parts)
                    logger.info(
                        f"knowledge base retrieved {len(results)} chunks "
                        f"for subject: {video_subject[:50]}"
                    )
                else:
                    kb_info["empty"] = True
                    logger.warning(
                        f"knowledge base returned empty results for: {video_subject[:50]}"
                    )
            else:
                kb_info["fallback"] = True
                logger.warning(
                    "knowledge base is unreachable, falling back to normal script generation"
                )
        except Exception as e:
            kb_info["fallback"] = True
            logger.error(f"knowledge base query failed: {e}, falling back")

    logger.info(
        "generating video script: "
        f"subject={video_subject}, paragraph_number={paragraph_number}, "
        f"target_duration={target_duration}, "
        f"has_custom_prompt={bool(video_script_prompt.strip())}, "
        f"has_custom_system_prompt={bool(custom_system_prompt.strip())}, "
        f"use_knowledge={use_knowledge}, kb_chunks={kb_info['chunks']}, "
        f"kb_fallback={kb_info['fallback']}"
    )

    def format_response(response):
        # Clean the script
        # Remove asterisks, hashes
        response = response.replace("*", "")
        response = response.replace("#", "")

        # Remove markdown syntax
        response = re.sub(r"\[.*\]", "", response)
        response = re.sub(r"\(.*\)", "", response)

        # Split the script into paragraphs
        paragraphs = response.split("\n\n")

        # Select the specified number of paragraphs
        # selected_paragraphs = paragraphs[:paragraph_number]

        # Join the selected paragraphs into a single string
        return "\n\n".join(paragraphs)

    def _generate_once(extra_prompt: str) -> str:
        """单次生成（内部沿用原有错误重试），返回去空白后的脚本或空串。"""
        prompt = build_script_prompt(
            video_subject=video_subject,
            language=language,
            paragraph_number=paragraph_number,
            video_script_prompt=video_script_prompt,
            custom_system_prompt=custom_system_prompt,
            knowledge_context=knowledge_context,
            target_duration=target_duration,
        )
        if extra_prompt:
            prompt += f"""

# Length Correction:
{extra_prompt}
""".rstrip()

        for i in range(_max_retries):
            try:
                response = _generate_response(prompt=prompt)
                if response:
                    final_script = format_response(response)
                else:
                    logging.error("gpt returned an empty response")

                # Some upstream providers may return quota errors as plain text.
                if final_script and "当日额度已消耗完" in final_script:
                    raise ValueError(final_script)

                if final_script:
                    return final_script.strip()
            except Exception as e:
                logger.error(f"failed to generate script: {e}")

            if i < _max_retries:
                logger.warning(f"failed to generate video script, trying again... {i + 1}")
        return ""

    if target_duration <= 0:
        final_script = _generate_once("")
        if "Error: " in final_script:
            logger.error(f"failed to generate video script: {final_script}")
        else:
            logger.success(f"completed: \n{final_script}")
        return final_script.strip(), kb_info

    # ── 时长闭环：生成 → 估算 → 超差回喂重写，取最接近目标的一次 ──
    tolerance = target_duration * SCRIPT_DURATION_TOLERANCE
    best_script = ""
    best_distance = None
    feedback = ""
    for attempt in range(1 + SCRIPT_DURATION_MAX_RETRIES):
        script = _generate_once(feedback)
        if not script:
            continue
        est = estimate_voiceover_duration(script)
        if est is None:
            est = 0.0
        distance = abs(est - target_duration)
        if best_distance is None or distance < best_distance:
            best_script = script
            best_distance = distance
        if distance <= tolerance:
            logger.success(f"script duration on target: {est:.1f}s / {target_duration}s")
            break
        feedback = (
            f"你上次生成的脚本预估朗读时长约 {est:.1f} 秒，"
            f"目标为 {target_duration} 秒左右。请{'压缩' if est > target_duration else '扩写'}"
            "内容，重写整段脚本，务必落在目标时长附近。"
        )
        logger.warning(
            f"script duration {est:.1f}s off target {target_duration}s "
            f"(attempt {attempt + 1}/{1 + SCRIPT_DURATION_MAX_RETRIES})"
        )

    best_est = estimate_voiceover_duration(best_script) if best_script else None
    logger.success(
        f"completed: target={target_duration}s, best={best_est if best_est is None else round(best_est, 1)}s\n"
        f"{best_script}"
    )
    return best_script.strip(), kb_info


def _strip_code_fence(text: str) -> str:
    """Strip a surrounding markdown code fence from an LLM response.

    Non-OpenAI providers (Claude, Gemini, …) frequently wrap JSON output in a
    ```json … ``` fence even when asked to return raw JSON. Removing it lets the
    first json.loads() succeed instead of falling through to the regex recovery
    path (and spuriously logging a warning). Mirrors the DOTALL handling already
    used in _parse_social_metadata().
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()



def translate_terms(terms: List[str]) -> List[str]:
    """Translate English search terms to Chinese for display annotation.
    Returns a list of Chinese translations matching the input order.
    On failure, returns empty strings so the caller can degrade gracefully.
    """
    if not terms:
        return []
    prompt = f"""# Role: Translator

Translate each of these English search terms into concise Chinese (2-8 characters each).
Return ONLY a JSON array of strings in the same order.

Terms: {json.dumps(terms, ensure_ascii=False)}

Output example: ["净水系统", "工业过滤", "反渗透膜"]
"""
    try:
        response = _generate_response(prompt)
        if response.startswith("Error:"):
            logger.warning(f"translate_terms failed: {response}")
            return [""] * len(terms)
        translations = json.loads(response.strip().lstrip("```json").rstrip("```").strip())
        if isinstance(translations, list) and len(translations) == len(terms):
            return translations
        logger.warning(f"translate_terms: unexpected response format: {response}")
        return [""] * len(terms)
    except Exception as e:
        logger.warning(f"translate_terms error: {e}")
        return [""] * len(terms)

def generate_terms(
    video_subject: str,
    video_script: str,
    amount: int = 5,
    match_script_order: bool = False,
    source: str = "pexels",
) -> List[str]:
    if match_script_order:
        goal = (
            f"Generate {amount} chronological stock-video search terms that follow "
            "the order of topics in the video script."
        )
        ordering_rule = (
            "6. keep the terms in the same order as the script narration; "
            "earlier terms must describe earlier visual moments."
        )
        # 有序关键词模式下，示例数量要和 amount 保持一致，避免模型被固定
        # 的 4 个示例误导，导致长文案只返回少量关键词，影响素材覆盖度。
        example_terms = [
            "opening visual topic",
            *[f"script visual topic {index}" for index in range(2, max(amount, 1))],
            "final visual topic",
        ]
        output_example = json.dumps(example_terms[:amount], ensure_ascii=False)
    else:
        goal = (
            f"Generate {amount} search terms for stock videos, depending on the "
            "subject of a video."
        )
        ordering_rule = ""
        output_example = (
            '["search term 1", "search term 2", "search term 3",'
            '"search term 4", "search term 5"]'
        )

    terms_lang = "Chinese" if source == "knowledge_base" else "English"
    prompt = f"""
# Role: Video Search Terms Generator

## Goals:
{goal}

## Constrains:
1. the search terms are to be returned as a json-array of strings.
2. each search term should consist of 1-3 words, always add the main subject of the video.
3. you must only return the json-array of strings. you must not return anything else. you must not return the script.
4. the search terms must be related to the subject of the video.
5. reply with search terms in {terms_lang}.
{ordering_rule}

## Output Example:
{output_example}

## Context:
### Video Subject
{video_subject}

### Video Script
{video_script}

Please note that you must use {terms_lang} for generating video search terms; other languages are not accepted.
""".strip()

    logger.info(f"subject: {video_subject}, match_script_order: {match_script_order}")

    search_terms = []
    response = ""
    for i in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if response.startswith("Error: "):
                # generate_terms 的公开返回类型是 List[str]。如果把 Provider 的
                # 错误文案原样返回，下游只做空值判断时会把非空字符串误认为成功，
                # 素材下载循环还会按字符遍历错误文案，产生无意义的外部请求。
                # 这里统一返回空列表，让任务编排层在真实故障位置立即结束任务。
                logger.error(f"failed to generate video terms: {response}")
                return []
            search_terms = json.loads(_strip_code_fence(response))
            if not isinstance(search_terms, list) or not all(
                isinstance(term, str) for term in search_terms
            ):
                logger.error("response is not a list of strings.")
                continue

        except Exception as e:
            logger.warning(f"failed to generate video terms: {str(e)}")
            if response:
                match = re.search(r"\[.*]", response, re.DOTALL)
                if match:
                    try:
                        search_terms = json.loads(match.group())
                    except Exception as e:
                        # 这里保留重试流程，但必须记录 LLM 返回的非标准 JSON，
                        # 否则后续排查搜索词为空时无法定位
                        # 是模型格式问题还是解析逻辑问题。
                        logger.warning(f"failed to generate video terms: {str(e)}")

        if search_terms and len(search_terms) > 0:
            break
        if i < _max_retries:
            logger.warning(f"failed to generate video terms, trying again... {i + 1}")

    logger.success(f"completed: \n{search_terms}")
    return search_terms


def generate_storyboard(
    video_subject: str,
    video_script: str,
) -> list:
    """根据视频脚本生成分镜表。

    将脚本按段落拆分，每段生成中英文搜索关键词，用于后续素材匹配。
    """
    paragraphs = [p.strip() for p in video_script.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [p.strip() for p in video_script.split("\n") if p.strip()]
    if len(paragraphs) <= 1:
        import re as _re2
        sentences = _re2.split(r"[。]", video_script)
        paragraphs = [s.strip() for s in sentences if s.strip()]

    if not paragraphs:
        return []

    n = len(paragraphs)
    logger.info(f"generating storyboard: {n} paragraphs, subject: {video_subject}")

    prompt_lines = [
        "# Role: Video Storyboard Generator",
        "",
        "You are creating a storyboard for a short video. For each paragraph of the script,",
        "generate search keywords in BOTH Chinese and English to find matching video footage.",
        "",
        "## Script Subject",
        video_subject,
        "",
        "## Script Paragraphs",
    ]
    for i, para in enumerate(paragraphs):
        prompt_lines.append(f"Paragraph {i+1}: {para}")

    prompt_lines.extend([
        "",
        "## Instructions",
        f"Return ONLY a JSON array with {n} objects (one per paragraph). Each object must have:",
        '- "keywords_cn": array of 2-3 Chinese search terms (2-8 chars each)',
        '- "keywords_en": array of 2-3 English search terms (1-3 words each)',
        "",
        "The keywords should describe the VISUAL content that best matches each paragraph.",
        "Consider: what would the camera show during this narration?",
        "",
        "## Output Format",
        "[",
        '  {',
        '    "keywords_cn": ["中文词1", "中文词2"],',
        '    "keywords_en": ["english term 1", "english term 2"]',
        '  },',
        "  ...",
        "]",
        "",
        "Return ONLY the JSON array, nothing else.",
    ])
    prompt = "\n".join(prompt_lines)

    storyboard = []
    response_text = ""
    for attempt in range(_max_retries):
        try:
            response_text = _generate_response(prompt)
            if response_text.startswith("Error: "):
                logger.error(f"storyboard generation failed: {response_text}")
                break
            data = json.loads(_strip_code_fence(response_text))
            if not isinstance(data, list) or len(data) != n:
                logger.warning(f"storyboard: expected {n} entries, got mismatch")
                continue

            for i, (para, shot) in enumerate(zip(paragraphs, data)):
                storyboard.append({
                    "index": i + 1,
                    "text": para,
                    "keywords_cn": shot.get("keywords_cn", []),
                    "keywords_en": shot.get("keywords_en", []),
                })
            break
        except Exception as e:
            logger.warning(f"storyboard parse error (attempt {attempt+1}): {e}")
            if response_text:
                import re as _re3
                match = _re3.search(r"\[.*\]", response_text, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group())
                        if isinstance(data, list):
                            for i, (para, shot) in enumerate(zip(paragraphs, data)):
                                if i >= len(data):
                                    break
                                storyboard.append({
                                    "index": i + 1,
                                    "text": para,
                                    "keywords_cn": shot.get("keywords_cn", []),
                                    "keywords_en": shot.get("keywords_en", []),
                                })
                            if storyboard:
                                break
                    except Exception:
                        pass  # regex recovery fallback

    if storyboard:
        logger.success(
            f"storyboard generated: {len(storyboard)} shots, "
            f"cn kw: {sum(len(s['keywords_cn']) for s in storyboard)}, "
            f"en kw: {sum(len(s['keywords_en']) for s in storyboard)}"
        )
    else:
        logger.warning("storyboard generation failed, will fallback to per-paragraph terms")

    return storyboard


def generate_jimeng_storyboard_and_script(
    video_subject: str,
    shots: list[dict],
    language: str = "",
    reuse_script: str = "",
) -> tuple[str, list[dict]]:
    """即梦模式：基于 Kimi 视觉描述统一创作「逐镜头画面提示词 + 分镜口播」。

    输入 shots（每项含 index / visual_description），让 LLM 一次产出：
      - 每个镜头的 camera_prompt（宣传片画面提示词：自主设计运镜 + 卖点，喂即梦图生视频）
      - script：N 段分镜口播，每段贴合对应镜头视觉描述，总时长 <30s（约 120-150 字）

    当 reuse_script 非空时（前端已预览并编辑过口播脚本），只重新生成 camera_prompt，
    复用传入的口播脚本，避免覆盖用户编辑。

    返回 (script, shots)，其中 shots 已回填 camera_prompt。失败抛异常，由上层转任务失败。
    """
    if not shots:
        raise ValueError("generate_jimeng_storyboard_and_script: empty shots")

    reuse_script = (reuse_script or "").strip()
    n = len(shots)
    shot_lines = []
    for s in shots:
        idx = s.get("index", 0)
        desc = (s.get("visual_description") or "").strip()
        shot_lines.append(
            f"镜头 {idx}：\n"
            f"  - 画面视觉描述：{desc or '（无描述，请依据主题自行发挥）'}"
        )

    prompt_lines = [
        "# Role: 宣传片分镜导演",
        "",
        "你正在为一支图生视频宣传片做分镜创作。下面是每个镜头的图片视觉描述（Kimi 视觉模型生成）。",
        "请为每个镜头自主设计一段「画面提示词」（camera_prompt），用于图生视频模型生成动态画面：",
        "   - 根据该镜头的视觉描述，自主决定运镜方式、画面运动方向与节奏",
        "   - 结合视频主题与产品卖点，突出产品细节与科技/工业质感，宣传片风格",
        "   - 各镜头运镜方式要有变化、避免连续重复，画面要有冲击力和宣传效果",
        "   - 60 字以内，中文，直接输出提示词正文",
    ]
    if reuse_script:
        prompt_lines.extend([
            "",
            "## 已确定的口播脚本（直接复用，无需改写）",
            reuse_script,
        ])
    else:
        prompt_lines.extend([
            "",
            "同时写一段「分镜口播脚本」（script），共 " + str(n) + " 段，每段贴合对应镜头的画面内容：",
            "   - 整段口播总时长控制在 30 秒以内（约 120-150 字）",
            "   - 每段一句，节奏紧凑，纯口播文案，无标题、无编号、无 markdown",
        ])
    prompt_lines.extend([
        "",
        "## 视频主题",
        video_subject,
        "",
        "## 镜头列表",
    ])
    prompt_lines.extend(shot_lines)
    prompt_lines.extend([
        "",
        "## 输出格式（严格 JSON，不要 markdown 代码围栏，不要多余文字）",
        "{",
        '  "shots": [',
        '    {"index": 1, "camera_prompt": "画面提示词"},',
        "    ...",
        "  ],",
    ])
    if reuse_script:
        prompt_lines.append('  "script": "（复用已给定的口播脚本，可留空字符串）"')
    else:
        prompt_lines.append('  "script": "第1段口播\\n第2段口播\\n..."')
    prompt_lines.append("}")
    prompt = "\n".join(prompt_lines)

    response_text = ""
    for attempt in range(_max_retries):
        try:
            response_text = _generate_response(prompt)
            if response_text.startswith("Error: "):
                logger.error(f"jimeng storyboard+script generation failed: {response_text}")
                break
            data = json.loads(_strip_code_fence(response_text))
            if not isinstance(data, dict):
                logger.warning(f"jimeng creative: expected dict, got {type(data).__name__}")
                continue
            shot_prompts = data.get("shots")
            script = reuse_script if reuse_script else (data.get("script") or "").strip()
            if not isinstance(shot_prompts, list) or len(shot_prompts) != n:
                logger.warning(
                    f"jimeng creative: expected {n} shot prompts, "
                    f"got {len(shot_prompts) if isinstance(shot_prompts, list) else type(shot_prompts).__name__}"
                )
                continue
            if not script:
                logger.warning("jimeng creative: empty script")
                continue
            for s, sp in zip(shots, shot_prompts):
                cp = sp.get("camera_prompt") if isinstance(sp, dict) else ""
                s["camera_prompt"] = (cp or "").strip() or s.get("visual_description", "")
            logger.success(f"jimeng creative: {n} shots + script generated")
            return script, shots
        except Exception as e:
            logger.warning(f"jimeng creative parse error (attempt {attempt + 1}): {e}")

    raise RuntimeError(
        f"generate_jimeng_storyboard_and_script failed after {_max_retries} attempts"
    )


def decompose_topic(
    video_subject: str,
    language: str = "zh",
) -> list:
    """将视频主题分解为相关的搜索词，供 KB 素材检索使用。

    与 generate_terms（从脚本文案提取关键词，可能偏离主题）不同，
    此函数将搜索词锚定在主题本身，确保素材相关性。

    返回 5-8 个中文搜索词，所有词限定在主题域内。
    """
    subject = (video_subject or "").strip()
    if not subject:
        logger.warning("decompose_topic: empty subject")
        return []

    logger.info(f"decomposing video topic: {subject[:80]}")

    prompt_lines = [
        "# Role: Search Query Decomposer",
        "",
        "You are an expert at breaking down a video topic into diverse search terms.",
        "Your goal is to find VISUAL footage, so think about what the camera would show.",
        "",
        "## Rules",
        "1. Generate 5-8 Chinese search terms (2-8 characters each)",
        "2. ALL terms MUST stay strictly within the topic domain — no drifting",
        "3. Terms should be diverse: different angles, aspects, or dimensions of the topic",
        "4. Think VISUALLY: what scenes, objects, locations match this topic?",
        "5. Return ONLY a JSON array of strings, nothing else",
        "",
        "## Examples",
        'Topic: "股市投资趋势分析"',
        'Output: ["股市投资", "股票交易", "金融数据", "投资分析", "证券市场", "经济趋势", "资本管理"]',
        "",
        'Topic: "人工智能技术发展"',
        'Output: ["人工智能", "机器学习", "数据中心", "机器人技术", "智能芯片", "算法编程", "神经网络"]',
        "",
        "## Your Turn",
        f"Topic: {subject}",
        "",
        "Return ONLY the JSON array:",
    ]
    prompt = "\\n".join(prompt_lines)

    response_text = ""
    for attempt in range(_max_retries):
        try:
            response_text = _generate_response(prompt)
            if response_text.startswith("Error: "):
                logger.error(f"decompose_topic LLM failed: {response_text}")
                break
            data = json.loads(_strip_code_fence(response_text))
            if not isinstance(data, list):
                logger.warning(f"decompose_topic: expected list, got {type(data).__name__}")
                continue
            # Filter: keep only strings, non-empty, strip whitespace
            terms = [str(t).strip() for t in data if t and str(t).strip()]
            # Deduplicate while preserving order
            seen = set()
            unique_terms = []
            for t in terms:
                if t not in seen:
                    seen.add(t)
                    unique_terms.append(t)
            if len(unique_terms) < 3:
                logger.warning(f"decompose_topic: too few terms ({len(unique_terms)}), retrying")
                continue
            # Keep at most 8
            result = unique_terms[:8]
            logger.success(
                f"decompose_topic: {len(result)} terms generated: {result}"
            )
            return result
        except Exception as e:
            logger.warning(f"decompose_topic parse error (attempt {attempt+1}): {e}")
            if response_text:
                import re as _re4
                match = _re4.search(r"\\[.*\\]", response_text, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group())
                        if isinstance(data, list):
                            terms = [str(t).strip() for t in data if t and str(t).strip()]
                            if len(terms) >= 3:
                                logger.success(
                                    f"decompose_topic: {len(terms)} terms (regex recovery)"
                                )
                                return terms[:8]
                    except Exception:
                        pass  # regex recovery fallback

    logger.warning("decompose_topic: all attempts failed, returning empty list")
    return []



# =============================================================================
# Social publishing metadata
#
# 根据视频主题和脚本生成发布到短视频平台时常用的 title、caption 和 hashtags。
# 这块能力只复用现有 LLM provider，不接入任何外部发布服务，也不影响视频生成主链路。
# =============================================================================

# 不同平台的文案长度和 hashtag 数量偏好不同。这里使用保守上限，避免模型返回
# 过长内容后调用方还需要二次裁剪。
SOCIAL_PLATFORMS = {
    "tiktok": {"title_max": 100, "caption_max": 2200, "hashtag_count": 5},
    "youtube_shorts": {"title_max": 100, "caption_max": 5000, "hashtag_count": 3},
    "instagram_reels": {"title_max": 125, "caption_max": 2200, "hashtag_count": 8},
    "facebook_reels": {"title_max": 125, "caption_max": 2200, "hashtag_count": 5},
}
DEFAULT_SOCIAL_PLATFORM = "tiktok"
DEFAULT_SOCIAL_LANGUAGE = "auto"
MAX_SOCIAL_SUBJECT_LENGTH = 500
MAX_SOCIAL_SCRIPT_LENGTH = 8000
MAX_SOCIAL_LANGUAGE_LENGTH = 64

SOCIAL_PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "youtube_shorts": "YouTube Shorts",
    "instagram_reels": "Instagram Reels",
    "facebook_reels": "Facebook Reels",
}

# LLM 不可用时的通用兜底标签。这里故意不绑定某个国家或语种，保证 API
# 对中文、英文、越南语等不同场景都能返回可用结构。
DEFAULT_SOCIAL_HASHTAGS = [
    "#shorts",
    "#viral",
    "#trending",
    "#fyp",
    "#video",
    "#reels",
    "#creator",
    "#content",
]


def _resolve_social_platform(platform: str | None) -> str:
    value = (platform or "").strip().lower()
    return value if value in SOCIAL_PLATFORMS else DEFAULT_SOCIAL_PLATFORM


def _normalize_social_language(language: str | None) -> str:
    value = (language or DEFAULT_SOCIAL_LANGUAGE).strip()
    if len(value) > MAX_SOCIAL_LANGUAGE_LENGTH:
        logger.warning(
            "social metadata language is too long and will be truncated to "
            f"{MAX_SOCIAL_LANGUAGE_LENGTH} characters."
        )
        value = value[:MAX_SOCIAL_LANGUAGE_LENGTH]
    return value or DEFAULT_SOCIAL_LANGUAGE


def _limit_social_text(text: str | None, max_length: int, field_name: str) -> str:
    value = (text or "").strip()
    if len(value) <= max_length:
        return value

    # API 层会限制长度；这里继续兜底，是为了保护内部调用或未来 WebUI
    # 直接调用时不会把超长内容发送给模型，避免 token 成本异常。
    logger.warning(
        f"{field_name} is too long and will be truncated to {max_length} characters."
    )
    return value[:max_length]


def _social_language_instruction(language: str | None) -> str:
    language = _normalize_social_language(language)
    if language.lower() == DEFAULT_SOCIAL_LANGUAGE:
        return (
            "Use the same language as the video subject and script. If the subject "
            "and script use different languages, prefer the script language."
        )

    return f'Write "title" and "caption" in this language: {language}.'


def _clamp_text(text, max_length: int) -> str:
    value = ("" if text is None else str(text)).strip()
    if max_length and len(value) > max_length:
        return value[:max_length].rstrip()
    return value


def _normalize_hashtags(raw, count: int) -> List[str]:
    """
    将 LLM 返回的 hashtag 统一整理成 `#tag` 格式。

    LLM 可能返回字符串、数组、带空格的词组、重复标签或包含标点的内容。
    这里集中清洗，可以让接口响应结构稳定，也避免平台发布时出现空标签、
    重复标签或不符合常见格式的 hashtag。
    """
    if isinstance(raw, str):
        candidates = re.split(r"[\s,]+", raw)
    elif isinstance(raw, (list, tuple)):
        # 数组里的每一项视为一个完整标签，因此 "du lich" 会变成
        # "#dulich"，而不是拆成两个标签。
        candidates = [str(entry) for entry in raw]
    else:
        candidates = []

    seen = set()
    result: List[str] = []
    for item in candidates:
        tag = re.sub(r"[^\w]", "", item, flags=re.UNICODE)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(f"#{tag}")
        if count and len(result) >= count:
            break
    return result


def build_social_metadata_prompt(
    video_subject: str,
    video_script: str = "",
    language: str = DEFAULT_SOCIAL_LANGUAGE,
    platform: str = DEFAULT_SOCIAL_PLATFORM,
) -> str:
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    platform = _resolve_social_platform(platform)
    spec = SOCIAL_PLATFORMS[platform]
    label = SOCIAL_PLATFORM_LABELS.get(platform, platform)
    language_instruction = _social_language_instruction(language)

    prompt = f"""
# Role: Short-Video Social Media Copywriter

## Goal
Write engaging publishing metadata for a short video that will be posted on {label}.

## Constraints
1. Respond ONLY with a single valid minified JSON object. No markdown, no code fences, no commentary.
2. The JSON must contain exactly these keys: "title", "caption", "hashtags".
3. "title": a catchy hook, at most {spec["title_max"]} characters.
4. "caption": an engaging description that ends with a call to action, at most {spec["caption_max"]} characters. Do not put hashtags inside the caption.
5. "hashtags": a JSON array of exactly {spec["hashtag_count"]} strings. Each must start with "#", contain no spaces, and be relevant to the topic and to {label}.
6. {language_instruction}

## Output Example
{{"title":"...","caption":"...","hashtags":["#example","#video"]}}

## Context
### Video Subject
{video_subject}

### Video Script
{video_script}
""".strip()
    return prompt


def _parse_social_metadata(response: str, platform: str) -> dict:
    spec = SOCIAL_PLATFORMS[_resolve_social_platform(platform)]

    data = None
    try:
        data = json.loads(_strip_code_fence(response))
    except Exception:
        # 部分模型会在 JSON 外层包一段说明文字或 markdown fence。
        # API 调用方只需要稳定结构，所以这里尝试提取第一个 JSON object。
        match = re.search(r"\{.*\}", response or "", re.DOTALL)
        if match:
            data = json.loads(match.group())

    if not isinstance(data, dict):
        raise ValueError("social metadata response is not a JSON object")

    title = _clamp_text(data.get("title", ""), spec["title_max"])
    caption = _clamp_text(data.get("caption", ""), spec["caption_max"])
    hashtags = _normalize_hashtags(data.get("hashtags", []), spec["hashtag_count"])

    if not title and not caption:
        raise ValueError("social metadata response is missing both title and caption")

    return {"title": title, "caption": caption, "hashtags": hashtags}


def _fallback_social_metadata(
    video_subject: str, video_script: str, platform: str
) -> dict:
    spec = SOCIAL_PLATFORMS[_resolve_social_platform(platform)]
    subject = (video_subject or "").strip()
    script = (video_script or "").strip()

    title = subject
    if not title and script:
        # 没有主题时，用脚本第一句兜底生成 title，避免接口返回空标题。
        title = re.split(r"(?<=[.!?。！？])\s+", script)[0]

    return {
        "title": _clamp_text(title, spec["title_max"]),
        "caption": _clamp_text(script or subject, spec["caption_max"]),
        "hashtags": _normalize_hashtags(DEFAULT_SOCIAL_HASHTAGS, spec["hashtag_count"]),
    }


def generate_social_metadata(
    video_subject: str,
    video_script: str = "",
    language: str = DEFAULT_SOCIAL_LANGUAGE,
    platform: str = DEFAULT_SOCIAL_PLATFORM,
) -> dict:
    """
    生成短视频发布文案元数据。

    返回结构固定为 `{"title": str, "caption": str, "hashtags": List[str]}`。
    如果 LLM 不可用或返回格式异常，会降级为通用启发式结果，保证 API
    调用方始终拿到可展示、可发布前编辑的数据结构。
    """
    platform = _resolve_social_platform(platform)
    language = _normalize_social_language(language)
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    prompt = build_social_metadata_prompt(
        video_subject=video_subject,
        video_script=video_script,
        language=language,
        platform=platform,
    )
    logger.info(f"generating social metadata: platform={platform}, language={language}")

    response = ""
    for i in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if isinstance(response, str) and "Error: " in response:
                logger.error(f"failed to generate social metadata: {response}")
                break
            metadata = _parse_social_metadata(response, platform)
            logger.success(f"completed: \n{metadata}")
            return metadata
        except Exception as e:
            logger.warning(f"failed to parse social metadata: {str(e)}")

        if i < _max_retries - 1:
            logger.warning(
                f"failed to generate social metadata, trying again... {i + 1}"
            )

    logger.warning("falling back to heuristic social metadata")
    return _fallback_social_metadata(video_subject, video_script, platform)


if __name__ == "__main__":
    video_subject = "生命的意义是什么"
    script = generate_script(
        video_subject=video_subject, language="zh-CN", paragraph_number=1
    )
    print("######################")
    print(script)
    search_terms = generate_terms(
        video_subject=video_subject, video_script=script, amount=5
    )
    print("######################")
    print(search_terms)


def _build_media_inventory(kb_media: list):
    """将素材清单构建为 LLM 可读的 inventory 文本 + media_id 映射表。

    Returns:
        (media_inventory_str, media_map, media_type_map)
        media_map: media_id -> filename
        media_type_map: media_id -> "image" | "video"
    """
    media_inventory = ""
    media_map = {}
    media_type_map = {}
    if not kb_media:
        return media_inventory, media_map, media_type_map
    lines = []
    for i, m in enumerate(kb_media, 1):
        m_name = m.get("name", m.get("path", f"unknown_{i}"))
        m_type = m.get("type", "image")
        m_desc = (m.get("description") or "").strip()
        m_id = f"media_{i}"
        media_map[m_id] = m_name
        media_type_map[m_id] = m_type
        if m_desc:
            lines.append(f"{m_id}: {m_name} ({m_type}) — {m_desc}")
        else:
            lines.append(f"{m_id}: {m_name} ({m_type})")
    media_inventory = "\n".join(lines)
    logger.info(f"script+storyboard: {len(kb_media)} media in inventory")
    return media_inventory, media_map, media_type_map


def _generate_storyboard_from_prompt(prompt, media_map, media_type_map, paragraph_number):
    """调 LLM 生成并解析 storyboard JSON（含重试 + 纯文本兜底）。"""
    response_text = ""
    storyboard = []
    for attempt in range(_max_retries):
        try:
            response_text = _generate_response(prompt=prompt)
            if not response_text or response_text.startswith("Error: "):
                logger.error(f"script+storyboard generation failed: {response_text}")
                continue
            cleaned = _strip_code_fence(response_text)
            data = json.loads(cleaned)
            if not isinstance(data, list) or len(data) == 0:
                logger.warning(f"script+storyboard: expected JSON array, got {type(data).__name__}")
                continue
            valid = True
            for i, item in enumerate(data):
                if not isinstance(item, dict):
                    valid = False
                    break
                para = str(item.get("paragraph", "")).strip()
                if not para:
                    logger.warning(f"script+storyboard: paragraph {i+1} is empty")
                    valid = False
                    break
                media_id = str(item.get("media", "")).strip()
                media_name = media_map.get(media_id, media_id) if media_map else ""
                media_type = media_type_map.get(media_id, "") if media_map else ""
                storyboard.append({
                    "index": i + 1,
                    "text": para,
                    "keywords_cn": item.get("keywords_cn", []) or [],
                    "keywords_en": item.get("keywords_en", []) or [],
                    "media": media_name,
                    "media_type": media_type,
                })
            if valid and storyboard:
                break
            else:
                storyboard = []
        except json.JSONDecodeError as e:
            logger.warning(f"script+storyboard JSON parse error (attempt {attempt+1}): {e}")
            if response_text:
                match = re.search(r"\[.*\]", response_text, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group())
                        if isinstance(data, list):
                            for i, item in enumerate(data):
                                if not isinstance(item, dict):
                                    continue
                                para = str(item.get("paragraph", "")).strip()
                                if not para:
                                    continue
                                media_id = str(item.get("media", "")).strip()
                                media_name = media_map.get(media_id, media_id) if media_map else ""
                                media_type = media_type_map.get(media_id, "") if media_map else ""
                                storyboard.append({
                                    "index": i + 1,
                                    "text": para,
                                    "keywords_cn": item.get("keywords_cn", []) or [],
                                    "keywords_en": item.get("keywords_en", []) or [],
                                    "media": media_name,
                                    "media_type": media_type,
                                })
                            if storyboard:
                                break
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"script+storyboard error (attempt {attempt+1}): {e}")
        if attempt < _max_retries - 1:
            logger.warning(f"retrying script+storyboard generation... {attempt + 2}")
    if storyboard:
        total_media = len(set(s["media"] for s in storyboard if s["media"]))
        logger.success(f"script+storyboard: {len(storyboard)} paragraphs, {total_media} unique media assigned")
    else:
        logger.error("script+storyboard: all attempts failed")
        if response_text:
            fallback = _parse_plain_script(response_text, paragraph_number)
            if fallback:
                storyboard = fallback
                logger.warning("script+storyboard: using plain-text fallback")
    return storyboard


def generate_script_with_storyboard(
    video_subject: str,
    language: str = "",
    paragraph_number: int = 5,
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    knowledge_context: str = "",
    kb_media: list | None = None,
) -> list:
    """一次 LLM 调用同时生成脚本+关键词+指定素材（图片/视频）的结构化输出。

    当 KB 模式激活时，将可用素材清单（图片+视频文件名）和知识库文档内容一起
    提交给 LLM，让 LLM 在生成脚本的同时为每段文案指定最匹配的一个素材文件。

    Returns:
        [{"index": 1, "text": "...", "keywords_cn": [...],
          "keywords_en": [...], "media": "xxx.jpg", "media_type": "image"}, ...]
        失败时返回空列表
    """
    import json as _json

    kb_media = kb_media or []
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )
    knowledge_context = _limit_script_text(
        knowledge_context, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH // 2, "knowledge_context"
    )

    # ---- Build media inventory ----
    media_inventory, media_map, media_type_map = _build_media_inventory(kb_media)

    # ---- Build system prompt ----
    system_prompt = (
        custom_system_prompt
        if custom_system_prompt
        else DEFAULT_SCRIPT_SYSTEM_PROMPT
    )

    # ---- Build user prompt ----
    prompt_parts = []
    prompt_parts.append(f"# Video Subject\n{video_subject}\n")

    if media_inventory:
        prompt_parts.append(
            "# Available Media\n"
            "Below are ALL media files (images and videos) available for this "
            "video. You MUST assign exactly one media to each paragraph. Choose "
            "the media that best visually matches the paragraph content. The "
            "(image)/(video) tag tells you each file's type.\n\n"
            f"{media_inventory}\n"
        )

    if knowledge_context:
        prompt_parts.append(
            "# Knowledge Base Facts\n"
            "The following is verified information from the knowledge base. "
            "You MUST base your script on these facts. Do not fabricate any "
            "information that contradicts the knowledge below.\n\n"
            f"{knowledge_context}\n"
        )

    if video_script_prompt:
        prompt_parts.append(
            f"# Additional Instructions\n{video_script_prompt}\n"
        )

    output_media_field = '"media": "media_1"'
    if not kb_media:
        output_media_field = '"media": ""'

    lang = language or "Chinese"

    prompt_parts.append(
        f"# Output Requirements\n"
        f"Generate a {paragraph_number}-paragraph video script in {lang}.\n\n"
        f"For EACH paragraph, assign ONE media file from the available list "
        f"that best matches the visual content. Each paragraph = one scene = "
        f"one media file.\n\n"
        f"Return ONLY a JSON array (no markdown fences, no explanation):\n"
        f'[\n'
        f'  {{\n'
        f'    "paragraph": "script text for paragraph 1...",\n'
        f'    "keywords_cn": ["chinese search term 1", "chinese search term 2"],\n'
        f'    "keywords_en": ["english term 1", "english term 2"],\n'
        f'    {output_media_field}\n'
        f'  }},\n'
        f'  ...\n'
        f']\n\n'
        f"## Critical Rules:\n"
        f"1. Return ONLY the JSON array - no markdown fences or extra text\n"
        f"2. Each paragraph MUST be self-contained narration text\n"
        f"3. Each paragraph MUST have exactly one assigned media from the "
        f"available list\n"
        f"4. keywords_cn: exactly 2-3 Chinese search terms (2-8 chars each)\n"
        f"5. keywords_en: exactly 2-3 English search terms (1-3 words each)\n"
        f"6. Do NOT reference the prompt, media IDs, or script structure in "
        f"the output text\n"
        f"7. Respond in {lang}\n"
        f"8. DO NOT assign all paragraphs the same media - distribute across "
        f"different media files\n"
    )

    prompt = "\n".join(prompt_parts)
    return _generate_storyboard_from_prompt(
        prompt, media_map, media_type_map, paragraph_number
    )


def generate_script_from_materials(
    kb_media: list,
    language: str = "",
    paragraph_number: int = 5,
    video_subject: str = "",
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    target_duration: int = 0,  # 目标口播时长（秒），0 = 不限制
    knowledge_context: str = "",  # 知识库事实文本（可选，注入为脚本约束）
) -> list:
    """素材驱动模式：根据勾选素材清单（文件名 + 视觉描述）生成脚本。

    与 generate_script_with_storyboard 的区别是「素材优先」：素材清单是主驱动，
    video_subject 只是可选线索。每段文案必须且只能指定一个素材。

    当 target_duration > 0 时开启时长闭环：生成后用估算器核对，超差回喂重写。

    Returns:
        与 generate_script_with_storyboard 相同的 storyboard 结构。
        失败时返回空列表。
    """
    kb_media = kb_media or []
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    # 素材驱动模式：段落数 = 实际用到的素材数量，不再沿用普通模式的
    # paragraph_number（默认 1），否则 40 个素材只会被用 1-3 个。
    # 时长优先：target_duration > 0 时按「每段最短 MIN_SECONDS_PER_PARAGRAPH 秒」
    # 尽量多分镜；未设时长时用足素材（受上限约束）。
    _n_media = len(kb_media)
    if _n_media > 0:
        if target_duration > 0:
            _n_para = max(1, target_duration // MAX_SECONDS_PER_PARAGRAPH)
        else:
            _n_para = _n_media
        paragraph_number = max(1, min(_n_para, _n_media, MAX_SCRIPT_PARAGRAPH_NUMBER))
    video_subject = _limit_script_text(
        video_subject, MAX_SCRIPT_PROMPT_LENGTH, "video_subject"
    )
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )
    knowledge_context = _limit_script_text(
        knowledge_context, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH // 2, "knowledge_context"
    )

    media_inventory, media_map, media_type_map = _build_media_inventory(kb_media)

    prompt_parts = []
    prompt_parts.append(
        "# Role\n"
        "You are creating a short-video script from a fixed set of media "
        "assets. These assets are the ONLY visual material available: every "
        "scene must show one of them.\n"
    )

    if custom_system_prompt:
        prompt_parts.append(f"# System Instructions\n{custom_system_prompt}\n")

    if media_inventory:
        prompt_parts.append(
            "# Available Media\n"
            "Below are ALL media files (images and videos) available. Each has a "
            "filename and a visual description. Assign exactly one media to each "
            "paragraph; the narration must describe what that media shows.\n\n"
            f"{media_inventory}\n"
        )
    else:
        prompt_parts.append("# Available Media\n(none provided)\n")

    if knowledge_context:
        prompt_parts.append(
            "# Knowledge Base Facts\n"
            "The following is verified information from the knowledge base. "
            "You MUST base your script on these facts. Do not fabricate any "
            "information that contradicts the knowledge below.\n\n"
            f"{knowledge_context}\n"
        )

    if video_subject:
        prompt_parts.append(
            f"# Video Topic (merge with the media descriptions)\n"
            f"Topic: {video_subject}\n"
            f"Merge this topic with the media visual descriptions: organize "
            f"the script around the topic as its narrative through-line, "
            f"while each paragraph describes the visual content of its "
            f"assigned media file. The topic decides what story to tell; "
            f"the media visuals provide the concrete scenes.\n"
        )

    if video_script_prompt:
        prompt_parts.append(
            f"# Additional Instructions\n{video_script_prompt}\n"
        )

    output_media_field = '"media": "media_1"' if kb_media else '"media": ""'
    lang = language or "Chinese"

    prompt_parts.append(
        f"# Output Requirements\n"
        f"Generate a {paragraph_number}-paragraph video script in {lang}.\n"
        f"{_script_duration_requirement(target_duration)}\n"
        f"{_per_paragraph_duration_requirement(target_duration, paragraph_number)}\n\n"
        f"Each paragraph MUST be grounded in one of the available media files: "
        f"the narration describes that media's visual content. Each paragraph = "
        f"one scene = one media file.\n\n"
        f"Return ONLY a JSON array (no markdown fences, no explanation):\n"
        f'[\n'
        f'  {{\n'
        f'    "paragraph": "script text for paragraph 1...",\n'
        f'    "keywords_cn": ["chinese search term 1", "chinese search term 2"],\n'
        f'    "keywords_en": ["english term 1", "english term 2"],\n'
        f'    {output_media_field}\n'
        f'  }},\n'
        f'  ...\n'
        f']\n\n'
        f"## Critical Rules:\n"
        f"1. Return ONLY the JSON array - no markdown fences or extra text\n"
        f"2. Each paragraph MUST describe the visual content of its assigned media\n"
        f"3. Each paragraph MUST have exactly one assigned media from the list\n"
        f"4. keywords_cn: exactly 2-3 Chinese search terms (2-8 chars each)\n"
        f"5. keywords_en: exactly 2-3 English search terms (1-3 words each)\n"
        f"6. Do NOT reference media IDs or script structure in the output text\n"
        f"7. Respond in {lang}\n"
        f"8. DO NOT assign all paragraphs the same media - distribute across "
        f"different media files\n"
    )

    prompt = "\n".join(prompt_parts)

    if target_duration <= 0:
        return _generate_storyboard_from_prompt(
            prompt, media_map, media_type_map, paragraph_number
        )

    # ── 时长闭环：生成 → 估算 → 超差回喂重写，取最接近目标的一次 ──
    tolerance = target_duration * SCRIPT_DURATION_TOLERANCE
    best_storyboard = []
    best_distance = None
    feedback = ""
    for attempt in range(1 + SCRIPT_DURATION_MAX_RETRIES):
        p = prompt
        if feedback:
            p += f"""

# Length Correction:
{feedback}
"""
        sb = _generate_storyboard_from_prompt(
            p, media_map, media_type_map, paragraph_number
        )
        if not sb:
            continue
        script_text = "\n".join(str(s.get("text", "")).strip() for s in sb)
        est = estimate_voiceover_duration(script_text)
        if est is None:
            est = 0.0
        distance = abs(est - target_duration)
        if best_distance is None or distance < best_distance:
            best_storyboard = sb
            best_distance = distance
        if distance <= tolerance:
            logger.success(
                f"material-driven script on target: {est:.1f}s / {target_duration}s"
            )
            break
        feedback = (
            f"你上次生成的脚本预估朗读时长约 {est:.1f} 秒，"
            f"目标为 {target_duration} 秒左右。请{'压缩' if est > target_duration else '扩写'}"
            f"内容（保持 {paragraph_number} 段不变，每段相应缩短/加长），"
            "务必落在目标时长附近，并继续为每段指定一个素材、尽量分散。"
        )
        logger.warning(
            f"material-driven script {est:.1f}s off target {target_duration}s "
            f"(attempt {attempt + 1}/{1 + SCRIPT_DURATION_MAX_RETRIES})"
        )

    return best_storyboard


def reassign_media_to_script(
    video_script: str,
    kb_media: list,
    language: str = "",
) -> list:
    """素材驱动模式：用户修改脚本后，在勾选素材集合内重新为每段匹配素材。

    关键保证：
    1. 文案逐字保真 —— text 一律取自输入段落，LLM 只负责指定 media，无权改写。
    2. 分段逻辑与下游 _parse_paragraph_durations 严格一致（空行 -> 换行 -> 句号切句）。
    3. media 仅限勾选集合（经 media_map 反查，越界/未知 media 落默认值）。

    Returns:
        与 _grounded_storyboard 相同的 storyboard 结构（含 text/media/media_type）。
    """
    kb_media = kb_media or []
    script = (video_script or "").strip()
    if not script:
        return []

    # 分段：与 generate_storyboard / _parse_paragraph_durations 保持一致
    paragraphs = [p.strip() for p in script.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [p.strip() for p in script.split("\n") if p.strip()]
    if len(paragraphs) <= 1:
        sentences = re.split(r"[。]", script)
        paragraphs = [s.strip() for s in sentences if s.strip()]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return []

    n = len(paragraphs)
    media_inventory, media_map, media_type_map = _build_media_inventory(kb_media)

    if not media_map:
        # 无素材可匹配：文案仍返回，media 置空，交给下游占位/空画面
        logger.warning("reassign_media_to_script: no media to assign")
        return [
            {
                "index": i + 1,
                "text": paragraphs[i],
                "keywords_cn": [],
                "keywords_en": [],
                "media": "",
                "media_type": "",
            }
            for i in range(n)
        ]

    media_ids = list(media_map.keys())
    default_media_id = media_ids[0]

    prompt_parts = [
        "# Role: Media Assignment",
        "",
        "You are matching already-written script paragraphs to a fixed set of "
        "media assets. DO NOT rewrite, rephrase, or reorder the script text. "
        "Your ONLY job is to choose the best media file for each paragraph.",
        "",
        "# Available Media",
        media_inventory,
        "",
        "# Script Paragraphs",
    ]
    for i, para in enumerate(paragraphs):
        prompt_parts.append(f"Paragraph {i+1}: {para}")

    prompt_parts.extend([
        "",
        "# Instructions",
        "Return ONLY a JSON array with exactly one object per paragraph. Each "
        "object must have:",
        '- "paragraph_index": the 1-based paragraph number',
        '- "media": the media id (e.g. "media_1") that best matches the visual content',
        "",
        "Rules:",
        "1. Do NOT change the paragraph text - it is fixed.",
        "2. Assign the media whose visual content best matches each paragraph.",
        "3. Assign a DISTINCT media to each paragraph. Do NOT reuse the same\n        media for different paragraphs unless there are more paragraphs than\n        available media.",
        "",
        "# Output Format",
        '[',
        '  {"paragraph_index": 1, "media": "media_1"},',
        '  {"paragraph_index": 2, "media": "media_2"}',
        ']',
        "",
        "Return ONLY the JSON array, nothing else.",
    ])
    prompt = "\n".join(prompt_parts)

    assignments = {}
    response_text = ""
    for attempt in range(_max_retries):
        try:
            response_text = _generate_response(prompt=prompt)
            if not response_text or response_text.startswith("Error: "):
                logger.error(f"reassign_media_to_script LLM failed: {response_text}")
                continue
            data = json.loads(_strip_code_fence(response_text))
            if not isinstance(data, list):
                logger.warning(
                    f"reassign_media_to_script: expected list, got {type(data).__name__}"
                )
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("paragraph_index", 0))
                except (TypeError, ValueError):
                    continue
                mid = str(item.get("media", "")).strip()
                if 1 <= idx <= n and mid in media_map:
                    assignments[idx] = mid
            if assignments:
                break
        except json.JSONDecodeError as e:
            logger.warning(
                f"reassign_media_to_script JSON parse error (attempt {attempt+1}): {e}"
            )
            if response_text:
                match = re.search(r"\[.*\]", response_text, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group())
                        if isinstance(data, list):
                            for item in data:
                                if not isinstance(item, dict):
                                    continue
                                try:
                                    idx = int(item.get("paragraph_index", 0))
                                except (TypeError, ValueError):
                                    continue
                                mid = str(item.get("media", "")).strip()
                                if 1 <= idx <= n and mid in media_map:
                                    assignments[idx] = mid
                            if assignments:
                                break
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"reassign_media_to_script error (attempt {attempt+1}): {e}")
        if attempt < _max_retries - 1:
            logger.warning(f"retrying media reassignment... {attempt + 2}")

    if not assignments:
        logger.warning(
            "reassign_media_to_script: LLM returned no valid assignments, using default"
        )
        assignments = {i + 1: media_ids[i % len(media_ids)] for i in range(n)}

    # 去重：同一素材不重复分配给多个段落；冲突时改派到未使用的素材，
    # 素材数 < 段落数时按使用次数最少循环复用，避免连续同一画面。
    used_count = {mid: 0 for mid in media_ids}
    final_assignments = {}
    for i in range(n):
        idx = i + 1
        mid = assignments.get(idx) or default_media_id
        if used_count.get(mid, 0) == 0:
            final_assignments[idx] = mid
            used_count[mid] = 1
            continue
        unused = [m for m in media_ids if used_count[m] == 0]
        if unused:
            final_assignments[idx] = unused[0]
            used_count[unused[0]] = 1
        else:
            least = min(media_ids, key=lambda m: used_count[m])
            final_assignments[idx] = least
            used_count[least] += 1

    # 补齐缺段 + 反查 media 名称/类型（文案逐字保真）
    storyboard = []
    for i in range(n):
        idx = i + 1
        mid = final_assignments.get(idx) or default_media_id
        storyboard.append({
            "index": idx,
            "text": paragraphs[i],
            "keywords_cn": [],
            "keywords_en": [],
            "media": media_map.get(mid, ""),
            "media_type": media_type_map.get(mid, ""),
        })

    logger.success(f"reassign_media_to_script: {n} paragraphs matched to media")
    return storyboard


def _parse_plain_script(text: str, paragraph_number: int) -> list:
    """Fallback: parse plain text response into basic storyboard structure."""
    text = text.replace("*", "").replace("#", "")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    paragraphs = paragraphs[:paragraph_number]
    if not paragraphs:
        return []
    return [
        {
            "index": i + 1,
            "text": p,
            "keywords_cn": [],
            "keywords_en": [],
            "media": "",
            "media_type": "",
        }
        for i, p in enumerate(paragraphs)
    ]
