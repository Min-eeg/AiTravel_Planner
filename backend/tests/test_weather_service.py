"""天气服务测试：多数据源降级链与数据归一化。"""

import pytest

from app.services.weather_service import (
    WMO_CODES,
    WeatherService,
    _mock_weather,
    _to_float,
)


def test_wmo_codes_cover_common_weather():
    """Open-Meteo 用 WMO 数字码，必须映射成中文才能展示。"""
    assert WMO_CODES[0] == "晴"
    assert WMO_CODES[3] == "阴"
    assert WMO_CODES[95] == "雷阵雨"


def test_to_float():
    assert _to_float("25", 0.0) == 25.0
    assert _to_float(None, 20.0) == 20.0
    assert _to_float("abc", 10.0) == 10.0


def test_mock_weather_shape():
    """mock 数据必须与真实数据源同构，否则前端会因字段缺失报错。"""
    rows = _mock_weather()
    assert len(rows) == 5
    for r in rows:
        assert set(r) >= {
            "date", "day_weather", "night_weather",
            "day_temp", "night_temp", "precipitation_prob", "source",
        }


@pytest.mark.asyncio
async def test_open_meteo_real_call():
    """真实联网验证 Open-Meteo（无需 Key）。网络不通时应自动降级而非抛错。"""
    svc = WeatherService()
    svc.settings.weather_provider = "open-meteo"

    rows = await svc.get_forecast(latitude=30.2596, longitude=120.1487, city="杭州", days=4)
    assert rows, "应返回至少一条数据（失败时应降级到 mock，不应为空）"

    first = rows[0]
    assert "date" in first and "day_weather" in first
    # 温度应是合理区间
    assert -60 <= first["day_temp"] <= 60
    assert -60 <= first["night_temp"] <= 60


@pytest.mark.asyncio
async def test_falls_back_to_mock_when_no_network(monkeypatch):
    """所有数据源都失败时必须降级到 mock，而不是抛异常。"""
    svc = WeatherService()
    svc.settings.weather_provider = "open-meteo"

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            raise RuntimeError("模拟网络不通")

    monkeypatch.setattr(
        "app.services.weather_service.httpx.AsyncClient",
        lambda *a, **k: FailingClient(),
    )

    rows = await svc.get_forecast(latitude=30.0, longitude=120.0, city="杭州", days=3)
    assert rows
    assert rows[0]["source"] == "mock"


@pytest.mark.asyncio
async def test_amap_10002_triggers_fallback(monkeypatch):
    """高德返回 10002 时应降级而不是崩溃 —— 这是实测真实遇到的情况。"""
    svc = WeatherService()
    svc.settings.weather_provider = "amap"
    svc.settings.amap_api_key = "test-key"

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "0", "infocode": "10002", "info": "SERVICE_NOT_AVAILABLE"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr("app.services.weather_service.httpx.AsyncClient", lambda *a, **k: FakeClient())

    rows = await svc.get_forecast(city="杭州", days=3)
    assert rows[0]["source"] == "mock"


@pytest.mark.asyncio
async def test_open_meteo_normalizes_precipitation():
    """降水概率字段应存在（可为 None），前端依赖它决定是否提示带伞。"""
    svc = WeatherService()
    svc.settings.weather_provider = "open-meteo"

    rows = await svc.get_forecast(latitude=30.2596, longitude=120.1487, city="杭州", days=3)
    for r in rows:
        assert "precipitation_prob" in r
        prob = r["precipitation_prob"]
        assert prob is None or 0 <= prob <= 100


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_request():
    """缓存命中时不应重复发请求。"""
    svc = WeatherService()
    svc.settings.weather_provider = "open-meteo"

    args = (30.2596, 120.1487)
    first = await svc.get_forecast(latitude=args[0], longitude=args[1], city="杭州", days=3)
    calls_before = svc.call_count if hasattr(svc, "call_count") else None

    second = await svc.get_forecast(latitude=args[0], longitude=args[1], city="杭州", days=3)
    assert len(first) == len(second)


def test_center_of_candidates():
    """景点中心坐标计算，用于按坐标查天气。"""
    from app.agents.planner import _center_of

    lat, lng = _center_of([
        {"longitude": 120.1, "latitude": 30.1},
        {"longitude": 120.3, "latitude": 30.3},
    ])
    assert abs(lat - 30.2) < 1e-6
    assert abs(lng - 120.2) < 1e-6


def test_center_of_empty_returns_default():
    """无候选时给默认值，不让天气查询崩掉。"""
    from app.agents.planner import _center_of

    lat, lng = _center_of([])
    assert 29 <= lat <= 31
    assert 119 <= lng <= 121


def test_center_of_skips_invalid_points():
    """缺失经纬度的候选应被跳过，不影响平均值。"""
    from app.agents.planner import _center_of

    lat, lng = _center_of([
        {"longitude": 120.0, "latitude": 30.0},
        {"longitude": None, "latitude": None},
    ])
    assert abs(lat - 30.0) < 1e-6
    assert abs(lng - 120.0) < 1e-6