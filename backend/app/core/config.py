"""配置管理：统一从环境变量 / .env 读取，全局单例。"""

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    """应用配置。刻意不引入 pydantic-settings，减少依赖与样板代码。"""

    def __init__(self) -> None:
        # ===== LLM =====
        self.llm_api_key: str = os.getenv("LLM_API_KEY", "")
        self.llm_base_url: str = os.getenv(
            "LLM_BASE_URL", "https://api.openai.com/v1"
        ).rstrip("/")
        self.llm_model_id: str = os.getenv("LLM_MODEL_ID", "gpt-4o-mini")
        self.llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "120"))
        self.llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "2"))

        # ===== 高德 =====
        self.amap_api_key: str = os.getenv("AMAP_API_KEY", "")

        # ===== 天气数据源 =====
        # open-meteo（默认，免费无需 Key，按坐标查询）/ amap（需账号开通天气权限）
        self.weather_provider: str = os.getenv("WEATHER_PROVIDER", "open-meteo").lower()

        # ===== 开关 =====
        self.rag_enabled: bool = os.getenv("RAG_ENABLED", "1") == "1"
        self.mock_mode: bool = os.getenv("MOCK_MODE", "0") == "1"
        self.cache_ttl: int = int(os.getenv("CACHE_TTL", "3600"))

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def has_amap(self) -> bool:
        return bool(self.amap_api_key)

    @property
    def use_mock(self) -> bool:
        """无 LLM Key 时强制走 mock，保证全链路可跑通。"""
        return self.mock_mode or not self.has_llm


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()