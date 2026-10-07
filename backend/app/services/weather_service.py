"""天气服务：多数据源，按经纬度查询。

为什么不用高德天气：
实测该账号的高德天气接口返回 10002（SERVICE_NOT_AVAILABLE），
用两个独立 Key 验证均如此，确认是账号级配额限制而非操作问题。

为什么 Open-Meteo 更适合本项目：
1. 免费且无需 Key，不受第三方配额影响
2. 按**经纬度**查询而非城市 adcode —— 我们已经有每个景点的真实坐标，
   拿到的天气比城市级更精准（同城不同景点可能略有差异）
3. 提供降水概率，这对"要不要带伞""几点出门"这类决策比温度更有用

保留高德作为可选数据源：账号权限开通后把 WEATHER_PROVIDER 改成 amap 即可，
业务代码和前端契约都不需要变。
"""

import datetime
import logging
from typing import Any, Dict, List, Optional

import httpx

from ..core.cache import cache, make_cache_key
from ..core.config import get_settings

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
AMAP_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherinfo"

# WMO weather code → 中文描述（Open-Meteo 用数字码表示天气）
WMO_CODES: Dict[int, str] = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "毛毛雨",
    53: "小雨",
    55: "中雨",
    56: "冻毛毛雨",
    57: "冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷阵雨伴冰雹",
}


class WeatherService:
    """天气查询。统一输出结构，屏蔽数据源差异。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def provider(self) -> str:
        return self.settings.weather_provider

    async def get_forecast(
        self,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        city: str = "",
        days: int = 5,
    ) -> List[Dict[str, Any]]:
        """查询逐日天气。

        优先按坐标查询（更精准）；没有坐标时回落到城市名。
        任何失败都降级到 mock，保证图表有数据可渲染。
        """
        if self.provider == "amap" and city:
            result = await self._from_amap(city)
            if result:
                return result
        elif self.provider == "open-meteo" and latitude is not None and longitude is not None:
            result = await self._from_open_meteo(latitude, longitude, days)
            if result:
                return result

        # 高德兜底：有时 open-meteo 网络不通
        if city and self.provider != "amap":
            result = await self._from_amap(city)
            if result:
                return result

        logger.info("天气数据源均不可用，降级为 mock")
        return _mock_weather()

    # ========== Open-Meteo ==========

    async def _from_open_meteo(
        self, latitude: float, longitude: float, days: int
    ) -> List[Dict[str, Any]]:
        cache_key = make_cache_key(
            "weather:om",
            lat=round(latitude, 3),
            lng=round(longitude, 3),
            days=days,
        )
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    OPEN_METEO_URL,
                    params={
                        "latitude": latitude,
                        "longitude": longitude,
                        "daily": (
                            "weather_code,temperature_2m_max,temperature_2m_min,"
                            "precipitation_probability_max,precipitation_sum"
                        ),
                        "timezone": "auto",
                        "forecast_days": max(1, min(days, 16)),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Open-Meteo 查询失败：%s", exc)
            return []

        daily = data.get("daily") or {}
        dates = daily.get("time") or []
        if not dates:
            return []

        codes = daily.get("weather_code", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])
        pop = daily.get("precipitation_probability_max", [])

        result = []
        for i, date_str in enumerate(dates):
            code = _at(codes, i)
            # 降水概率有值时带上，供前端展示"要不要带伞"
            prob = _at(pop, i)
            result.append(
                {
                    "date": date_str,
                    "day_weather": WMO_CODES.get(code, "未知"),
                    "night_weather": WMO_CODES.get(code, "未知"),
                    "day_temp": float(_at(tmax, i, 25.0)),
                    "night_temp": float(_at(tmin, i, 15.0)),
                    "precipitation_prob": int(prob) if prob is not None else None,
                    "source": "open-meteo",
                }
            )

        cache.set(cache_key, result)
        return result

    # ========== 高德（可选） ==========

    async def _from_amap(self, city: str) -> List[Dict[str, Any]]:
        if not self.settings.amap_api_key:
            return []

        cache_key = make_cache_key("weather:amap", city=city)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    AMAP_WEATHER_URL,
                    params={
                        "key": self.settings.amap_api_key,
                        "city": city,
                        "extensions": "all",
                        "output": "JSON",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("高德天气查询失败：%s", exc)
            return []

        if data.get("status") != "1":
            logger.info("高德天气不可用：%s", data.get("info"))
            return []

        forecasts = data.get("forecasts") or []
        if not forecasts:
            return []

        casts = forecasts[0].get("casts", [])
        result = [
            {
                "date": c.get("date", ""),
                "day_weather": c.get("dayweather", ""),
                "night_weather": c.get("nightweather", ""),
                "day_temp": _to_float(c.get("daytemperature"), 25.0),
                "night_temp": _to_float(c.get("nighttemperature"), 15.0),
                "precipitation_prob": None,
                "source": "amap",
            }
            for c in casts
        ]

        cache.set(cache_key, result)
        return result


def _at(seq: List[Any], index: int, default: Any = None) -> Any:
    return seq[index] if index < len(seq) else default


def _to_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return fallback


def _mock_weather() -> List[Dict[str, Any]]:
    """兜底数据（无网络时），格式与其他数据源一致。"""
    base = datetime.date.today()
    conditions = [
        ("晴", 26, 17, 0),
        ("多云", 23, 16, 10),
        ("小雨", 19, 15, 70),
        ("阴", 22, 16, 20),
    ]
    result = []
    for i in range(5):
        weather, d_t, n_t, prob = conditions[i % len(conditions)]
        result.append(
            {
                "date": (base + datetime.timedelta(days=i)).isoformat(),
                "day_weather": weather,
                "night_weather": weather,
                "day_temp": float(d_t),
                "night_temp": float(n_t),
                "precipitation_prob": prob,
                "source": "mock",
            }
        )
    return result


_service: Optional[WeatherService] = None


def get_weather_service() -> WeatherService:
    global _service
    if _service is None:
        _service = WeatherService()
    return _service