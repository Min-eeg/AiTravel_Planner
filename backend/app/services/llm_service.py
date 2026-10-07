"""LLM 服务：基于 LangChain ChatOpenAI 的结构化输出。

为什么不用手写 HTTP + JSON 解析：
- `with_structured_output(PydanticModel)` 由 LangChain 处理了
  函数调用 / JSON mode / 解析失败重试，我们不该重复造这一层
- 结构化输出约束由 Pydantic schema 表达，比提示词里写"请只输出 JSON"可靠得多

关于 self-correction：LangChain 的 `with_structured_output` 内部已含
解析失败重试；我们额外保留一层显式修复，把校验错误回传给模型，
这样"AI 输出 JSON 不稳定"这个经典问题有可解释的处理链路（面试可讲）。
"""

import logging
from typing import Any, Dict, List, Optional, Type, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from ..core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """LLM 调用或结构化输出失败。"""


class LLMUnavailableError(LLMError):
    """服务不可用：账户欠费、鉴权失败、模型不存在等。

    这类错误重试无意义（不是瞬时抖动），必须立刻上抛，
    让上层直接降级并明确告知用户原因，而不是反复重试后给一个含糊的失败提示。
    """


# 从错误文本里识别「不可重试」的原因
_FATAL_PATTERNS = (
    ("arrearage", "账户欠费，请在阿里云百炼控制台充值后重试"),
    ("access denied", "API Key 无访问权限，请检查密钥与模型授权"),
    ("invalid api-key", "API Key 无效，请检查 LLM_API_KEY 是否正确"),
    ("unauthorized", "鉴权失败，请检查 API Key"),
    ("model_not_exist", "模型不存在，请检查 LLM_MODEL_ID 是否正确"),
    ("model not exist", "模型不存在，请检查 LLM_MODEL_ID 是否正确"),
    ("insufficient balance", "账户余额不足，请充值后重试"),
    ("quota exhausted", "调用额度已用尽，请等待额度重置或更换模型"),
)


def _classify_error(text: str) -> LLMError:
    """按错误内容区分「可重试」与「不可重试」。

    可重试的（超时、5xx、限流）返回普通 LLMError，让上层重试；
    账户/鉴权类返回 LLMUnavailableError，上层立刻降级。
    """
    lowered = text.lower()
    for needle, friendly in _FATAL_PATTERNS:
        if needle in lowered:
            return LLMUnavailableError(friendly)
    return LLMError(text)


class LLMService:
    """封装 ChatOpenAI，提供结构化输出能力。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._client: Optional[ChatOpenAI] = None
        self.call_count = 0

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_api_key)

    def _get_client(self) -> ChatOpenAI:
        if not self.available:
            raise LLMError("未配置 LLM_API_KEY")
        if self._client is None:
            self._client = ChatOpenAI(
                api_key=self.settings.llm_api_key,
                base_url=self.settings.llm_base_url,
                model=self.settings.llm_model_id,
                temperature=0.4,
                timeout=self.settings.llm_timeout,
                max_retries=self.settings.llm_max_retries,
            )
        return self._client

    async def structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Type[T],
        repair_attempts: int = 1,
    ) -> T:
        """按 Pydantic schema 生成结构化输出。

        三段式容错链的中间环节：
            正常返回 → schema 校验通过 → 返回实例
            校验失败 → 把错误信息回传给模型，让它自我纠正
            仍失败 → 抛 LLMError，由上层降级为模板计划
        """
        if not self.available:
            raise LLMError("未配置 LLM_API_KEY")

        client = self._get_client()

        # DashScope（通义千问）的 json_object 模式有个硬性约束：
        # messages 里必须出现 "json" 字样，否则直接返回 400。
        # 提前补上，避免每次调用都踩这个坑。
        system_prompt = _ensure_json_hint(system_prompt)
        user_prompt = _ensure_json_hint(user_prompt)

        prompt = ChatPromptTemplate.from_messages(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        chain = prompt | client.with_structured_output(schema)

        messages: List[Any] = []
        last_error: Optional[str] = None

        for attempt in range(repair_attempts + 1):
            try:
                result = await chain.ainvoke(
                    {"input": _render(user_prompt, messages)}
                )
                self.call_count += 1
                return result
            except Exception as exc:
                last_error = str(exc)[:300]
                logger.warning("结构化输出失败（第 %s 次）：%s", attempt + 1, last_error)
                if attempt < repair_attempts:
                    # 自我纠正：把错误原因回传，让模型带着错误信息重试
                    messages.append(last_error)

        raise LLMError(f"结构化输出生成失败：{last_error}")

    async def text(self, system_prompt: str, user_prompt: str) -> str:
        """普通文本生成。"""
        client = self._get_client()
        resp = await client.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        self.call_count += 1
        return resp.content if hasattr(resp, "content") else str(resp)


JSON_HINT = "输出必须是合法的 JSON 对象，不要包含任何解释文字或markdown 代码块标记。"


def _ensure_json_hint(text: str) -> str:
    """确保提示词里含 "json" 字样，兼容 DashScope 的 json_object 模式约束。

    DashScope 要求 messages 中必须出现 json 字样，否则报 400。
    大小写不敏感，因此这里同时匹配 JSON / Json / json。
    """
    if "json" in text.lower():
        return text
    return f"{text}\n\n{JSON_HINT}"


def _render(user_prompt: str, errors: List[str]) -> str:
    """把上轮错误拼到提示词末尾，实现 self-correction。"""
    if not errors:
        return user_prompt
    correction = "\n\n".join(
        f"【上一次输出的问题】{err}\n请针对上述问题修正并重新输出完整结果。"
        for err in errors
    )
    return f"{user_prompt}\n\n{correction}"


_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    global _service
    if _service is None:
        _service = LLMService()
    return _service