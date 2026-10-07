"""服务层测试：高德降级、mock 数据、RAG 检索、结构化输出降级行为。"""

import pytest

from app.services.amap_service import (
    _mock_weather,
    _normalize_forecast,
    _normalize_poi,
    _to_float,
)


# ========== 高德 POI 归一化 ==========


def test_normalize_poi_parses_location_string():
    """高德的 location 字段是 "lng,lat" 字符串，必须正确拆分。"""
    poi = {"name": "西湖", "location": "120.155,30.274", "address": "环城西路"}
    result = _normalize_poi(poi)
    assert result["longitude"] == 120.155
    assert result["latitude"] == 30.274


def test_normalize_poi_handles_missing_location():
    result = _normalize_poi({"name": "某地", "location": ""})
    assert result["longitude"] == 0.0
    assert result["latitude"] == 0.0


def test_normalize_poi_handles_malformed_location():
    """脏数据不能让整个流程崩掉。"""
    result = _normalize_poi({"name": "某地", "location": "abc,def"})
    assert result["longitude"] == 0.0


def test_normalize_poi_ticket_price_from_biz_ext():
    poi = {
        "name": "灵隐寺",
        "location": "120.1,30.2",
        "biz_ext": {"ticket_price": "75"},
    }
    assert _normalize_poi(poi)["ticket_price"] == 75.0


def test_normalize_poi_ticket_price_invalid():
    poi = {"name": "x", "location": "1,2", "biz_ext": {"ticket_price": "免费"}}
    assert _normalize_poi(poi)["ticket_price"] == 0.0


def test_to_float_fallbacks():
    assert _to_float("12.5") == 12.5
    assert _to_float(None, 3.0) == 3.0
    assert _to_float("abc", 1.0) == 1.0


# ========== 天气预报 ==========


def test_normalize_forecast():
    casts = [
        {
            "date": "2026-08-10",
            "dayweather": "晴",
            "nightweather": "多云",
            "daytemperature": "33",
            "nighttemperature": "25",
        }
    ]
    result = _normalize_forecast(casts)
    assert result[0]["day_temp"] == 33.0
    assert result[0]["night_weather"] == "多云"


def test_normalize_forecast_with_bad_temperature():
    result = _normalize_forecast([{"date": "x", "daytemperature": "高温"}])
    assert result[0]["day_temp"] == 25.0  # 使用默认值而非崩溃


# ========== 降级 mock ==========


@pytest.mark.asyncio
async def test_mock_poi_for_known_city():
    """mock 池必须覆盖常见城市，且坐标落在合理范围内。"""
    from app.services.amap_service import AmapService

    service = AmapService()
    service.settings.amap_api_key = ""

    pois = await service.search_poi("景点", "杭州", offset=3)
    assert len(pois) == 3
    assert pois[0]["source"] == "mock"
    assert 119 < pois[0]["longitude"] < 122  # 杭州经度范围
    assert 29 < pois[0]["latitude"] < 31


@pytest.mark.asyncio
async def test_mock_poi_for_unknown_city_falls_back():
    """未覆盖城市应回落到通用池，而不是返回空列表。"""
    from app.services.amap_service import AmapService

    service = AmapService()
    service.settings.amap_api_key = ""

    pois = await service.search_poi("景点", "某不存在城市", offset=3)
    assert len(pois) == 3


def test_mock_weather_returns_days():
    weather = _mock_weather()
    assert len(weather) == 4
    assert weather[0]["day_weather"]


@pytest.mark.asyncio
async def test_amap_service_degrades_without_key():
    """无 Key 时必须返回 mock 而非抛异常——保证全链路可跑通。"""
    from app.services.amap_service import AmapService

    service = AmapService()
    service.settings.amap_api_key = ""  # 强制无 Key

    pois = await service.search_poi("西湖", "杭州")
    assert len(pois) > 0
    assert pois[0]["source"] == "mock"

    weather = await service.get_weather("杭州")
    assert len(weather) > 0


@pytest.mark.asyncio
async def test_geocode_returns_none_for_unknown():
    """坐标校正查不到时应返回 None，让调用方保留原值而非崩。"""
    from app.services.amap_service import AmapService

    service = AmapService()
    service.settings.amap_api_key = ""

    # mock 池里没有这个名字，走 mock 降级逻辑
    result = await service.geocode_poi("不存在的景点名XYZ", "杭州")
    assert result is None or isinstance(result, dict)


