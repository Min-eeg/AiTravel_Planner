"""高德地图 Web 服务封装（REST 直连，非 MCP）。

为什么 REST 直连而不是 MCP：
- 行程生成是高频阻塞调用，REST + 连接复用比每次起子进程更轻
- 需要精确控制超时与重试，REST 层更可控
- 同时支持真实 / mock 双模式，无 Key 时可跑通全链路

所有外部查询统一走缓存，避免重复消耗配额。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from ..core.cache import cache, make_cache_key
from ..core.config import get_settings

logger = logging.getLogger(__name__)

AMAP_BASE = "https://restapi.amap.com/v3"

# 无 Key 时的 mock 景点池，按城市粗略分组，保证 demo 可跑
_MOCK_POOL: Dict[str, List[Dict[str, Any]]] = {
    "北京": [
        ("故宫博物院", 116.397, 39.918, 60, "明清两代皇家宫殿，世界现存最大木结构宫殿群"),
        ("天坛公园", 116.407, 39.882, 34, "明清皇帝祭天之处，古柏成荫"),
        ("颐和园", 116.276, 39.999, 30, "皇家园林，长廊与昆明湖"),
        ("什刹海", 116.386, 39.938, 0, "老北京水岸风情与胡同生活"),
        ("雍和宫", 116.418, 39.949, 25, "北京香火最旺的藏传佛教寺院"),
    ],
    "上海": [
        ("外滩", 121.490, 31.241, 0, "万国建筑博览群与陆家嘴天际线"),
        ("豫园", 121.492, 31.227, 40, "明代江南园林与商城"),
        ("武康路", 121.434, 31.210, 0, "梧桐树下的老洋房街区"),
        ("上海博物馆", 121.475, 31.229, 0, "青铜与书画收藏重镇"),
        ("田子坊", 121.468, 31.210, 0, "泰康路创意街区与工作室"),
    ],
    "杭州": [
        ("西湖·断桥白堤", 120.149, 30.259, 0, "白堤步行与断桥残雪"),
        ("灵隐寺", 120.101, 30.241, 75, "江南名刹，飞来峰石刻"),
        ("龙井村", 120.127, 30.227, 0, "茶山与炒茶手艺"),
        ("浙江省博物馆", 120.145, 30.246, 0, "免费预约，浙江历史文化"),
        ("河坊街", 120.168, 30.241, 0, "南宋御街与老字号"),
    ],
    "成都": [
        ("宽窄巷子", 104.057, 30.669, 0, "清代满城遗韵的三条街巷"),
        ("大熊猫繁育研究基地", 104.146, 30.733, 55, "需早预约，观熊猫"),
        ("锦里古街", 104.043, 30.644, 0, "武侯祠旁的民俗商业街"),
        ("杜甫草堂", 104.027, 30.660, 50, "唐代诗人故居与园林"),
        ("春熙路", 104.081, 30.657, 0, "成都商业核心与美食聚集"),
    ],
    "长沙": [
        ("橘子洲头", 112.963, 28.192, 0, "湘江中的洲，青年毛泽东雕像"),
        ("岳麓山", 112.939, 28.185, 0, "山麓书院与爱晚亭"),
        ("五一广场", 112.977, 28.193, 0, "长沙城市中心与小吃"),
        ("湖南博物院", 112.989, 28.191, 0, "马王堆汉墓与国宝展品"),
        ("太平老街", 112.972, 28.197, 0, "保留完整的千年古街"),
    ],
}

_DEFAULT_POOL = [
    ("城市博物馆", 114.30, 30.59, 0, "了解城市历史的首选"),
    ("城市公园", 114.28, 30.60, 0, "适合慢行与休憩"),
    ("中央步行街", 114.29, 30.58, 0, "本地生活与小吃聚集"),
    ("文创园区", 114.31, 30.57, 0, "旧厂房改造的艺术空间"),
    ("古文化街", 114.30, 30.61, 0, "传统建筑与地方小吃"),
]


class AmapService:
    """高德 REST 服务。所有方法在无 Key / 请求失败时降级返回 mock 数据。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = AMAP_BASE

    @property
    def available(self) -> bool:
        return bool(self.settings.amap_api_key)

    async def _get(self, path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """带缓存的高德 GET 请求。返回 None 表示不可用，调用方负责降级。"""
        if not self.available:
            return None

        params = {**params, "key": self.settings.amap_api_key}
        cache_key = make_cache_key(f"amap:{path}", **params)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.base_url}/{path}", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # 网络/解析异常一律降级，不阻断主流程
            logger.warning("高德接口 %s 调用失败：%s", path, exc)
            return None

        if data.get("status") != "1":
            logger.warning("高德接口 %s 返回异常：%s", path, data.get("info"))
            return None

        cache.set(cache_key, data)
        return data

    # ========== POI 搜索 ==========

    async def search_poi(
        self, keywords: str, city: str, offset: int = 8
    ) -> List[Dict[str, Any]]:
        """搜索 POI，返回归一化结果。失败降级到 mock 池。"""
        data = await self._get(
            "place/text",
            {
                "keywords": keywords,
                "city": city,
                "citylimit": "true",
                "extensions": "all",
                "offset": str(offset),
            },
        )
        if data and data.get("pois"):
            return [_normalize_poi(p) for p in data["pois"]]
        return self._mock_poi(city, offset)

    def _mock_poi(self, city: str, offset: int) -> List[Dict[str, Any]]:
        """降级数据源：无 Key 或接口失败时使用。

        mock 模式没有真实景点图片，用高德静态图服务生成占位图，
        这样无 Key 时前端也能验证图片渲染效果。
        """
        from random import randint

        pool = _MOCK_POOL.get(city, _DEFAULT_POOL)
        return [
            {
                "name": name,
                "address": f"{city}{name}",
                "longitude": lng,
                "latitude": lat,
                "ticket_price": ticket,
                "description": desc,
                "image_url": (
                    "https://restapi.amap.com/v3/staticmap"
                    f"?location={lng},{lat}&zoom=14&size=300*200"
                    f"&markers=mid,{randint(0, 0xFFFFFF):06x}"
                ),
                "source": "mock",
            }
            for name, lng, lat, ticket, desc in pool[:offset]
        ]

    async def get_weather(self, city: str) -> List[Dict[str, Any]]:
        """查询天气。返回归一化的逐日天气，失败降级到 mock。"""
        data = await self._get(
            "weather/weatherinfo",
            {"city": city, "extensions": "all", "output": "JSON"},
        )
        if data:
            forecasts = data.get("forecasts") or []
            if forecasts:
                cast = forecasts[0].get("casts", [])
                return _normalize_forecast(cast)
        return _mock_weather()

    # ========== 坐标校正 ==========

    async def geocode_poi(self, name: str, city: str) -> Optional[Dict[str, float]]:
        """按「景点名 + 城市」回查真实坐标。

        LLM 生成的经纬度基本不可信，这个方法是地图准确性的关键保障。
        查不到返回 None，调用方保留原值即可，不阻断主流程。
        """
        pois = await self.search_poi(keywords=name, city=city, offset=1)
        if not pois:
            return None
        first = pois[0]
        return {
            "longitude": float(first["longitude"]),
            "latitude": float(first["latitude"]),
        }


def _normalize_poi(poi: Dict[str, Any]) -> Dict[str, Any]:
    """把高德原始 POI 结构归一化。location 字段是 "lng,lat" 字符串。"""
    location = poi.get("location", "")
    lng, lat = 0.0, 0.0
    if "," in location:
        parts = location.split(",")
        try:
            lng, lat = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            pass

    ticket = 0.0
    try:
        # 高德票价字段在不同接口下形态不一致，统一容错
        raw_ticket = poi.get("biz_ext", {}).get("ticket_price", "0")
        ticket = float(raw_ticket)
    except (ValueError, TypeError, AttributeError):
        ticket = 0.0

    # 取第一张真实照片：POI 搜索的extensions=all 会返回 photos 列表
    image_url = ""
    photos = poi.get("photos") or []
    if photos:
        first = photos[0]
        image_url = first.get("url", "") if isinstance(first, dict) else ""
    # 高德部分图片仍是 http://，前端页面为 https 时会被浏览器按混合内容拦截，
    # 这里统一升级为 https（实测 store.is.autonavi.com 支持 https）
    if image_url.startswith("http://"):
        image_url = "https://" + image_url[7:]

    return {
        "name": poi.get("name", ""),
        "address": poi.get("address", ""),
        "longitude": lng,
        "latitude": lat,
        "ticket_price": ticket,
        "rating": float(poi.get("rating", "0") or 0),
        "description": "",
        "image_url": image_url,
        "source": "amap",
    }


def _normalize_forecast(casts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """归一化天气预报。"""
    result = []
    for c in casts:
        day = c.get("dayweather", "")
        night = c.get("nightweather", "")
        result.append(
            {
                "date": c.get("date", ""),
                "day_weather": day,
                "night_weather": night,
                "day_temp": _to_float(c.get("daytemperature"), 25),
                "night_temp": _to_float(c.get("nighttemperature"), 15),
            }
        )
    return result


def _mock_weather() -> List[Dict[str, Any]]:
    """无网络时的兜底天气，保证图表有数据可渲染。"""
    import datetime

    base = datetime.date.today()
    conditions = [("晴", "晴", 26, 17), ("多云", "阴", 23, 16), ("小雨", "阴", 19, 15)]
    result = []
    for i in range(4):
        day_w, night_w, d_t, n_t = conditions[i % len(conditions)]
        result.append(
            {
                "date": (base + datetime.timedelta(days=i)).isoformat(),
                "day_weather": day_w,
                "night_weather": night_w,
                "day_temp": d_t,
                "night_temp": n_t,
            }
        )
    return result


def _to_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return fallback


_service: Optional[AmapService] = None


def get_amap_service() -> AmapService:
    """全局单例。"""
    global _service
    if _service is None:
        _service = AmapService()
    return _service