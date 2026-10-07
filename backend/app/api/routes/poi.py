"""POI 与地图路由：供前端补充查询、调试工具使用。"""

import logging

from fastapi import APIRouter, Query

from ...services.amap_service import get_amap_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["poi"])


@router.get("/poi/search")
async def search_poi(
    keywords: str = Query(..., description="搜索关键词"),
    city: str = Query(..., description="城市"),
    offset: int = Query(8, ge=1, le=25, description="返回条数"),
) -> dict:
    """搜索 POI。"""
    pois = await get_amap_service().search_poi(keywords, city, offset)
    return {"total": len(pois), "items": pois}


@router.get("/map/weather")
async def get_weather(city: str = Query(..., description="城市名")) -> dict:
    """查询天气。"""
    forecasts = await get_amap_service().get_weather(city)
    return {"city": city, "forecasts": forecasts}


@router.get("/map/geocode")
async def geocode(
    name: str = Query(..., description="地点名称"),
    city: str = Query(..., description="城市"),
) -> dict:
    """地理编码：把地点名解析为真实坐标。

    这是坐标校正能力的对外暴露，前端可用来验证某个景点的坐标是否可信。
    """
    location = await get_amap_service().geocode_poi(name, city)
    if not location:
        return {"found": False, "name": name, "city": city}
    return {"found": True, "name": name, "city": city, **location}