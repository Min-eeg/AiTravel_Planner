"""pytest 公共夹具。"""

import sys
from pathlib import Path

import pytest

# 保证测试能从 backend 目录导入 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def sample_request():
    from app.models.schemas import TripRequest

    return TripRequest(
        city="杭州",
        start_date="2026-08-10",
        days=2,
        preferences=["历史文化", "美食"],
        budget_level="中等",
        pace="适中",
        free_text="",
    )


@pytest.fixture
def sample_plan():
    """一份结构完整、必然通过全部硬指标的行程。"""
    from app.agents.graph import settle_budget
    from app.models.schemas import (
        Attraction,
        DayPlan,
        Location,
        Meal,
        TripPlan,
        WeatherInfo,
    )

    days = []
    for i in range(2):
        days.append(
            DayPlan(
                date=f"2026-08-{10 + i:02d}",
                day_index=i,
                title=f"第{i + 1}天",
                description="正常行程",
                attractions=[
                    Attraction(
                        name=f"景点{i + 1}",
                        location=Location(longitude=120.15, latitude=30.25),
                        ticket_price=60.0,
                    )
                ],
                meals=[
                    Meal(type="breakfast", name="早餐", estimated_cost=30.0),
                    Meal(type="lunch", name="午餐", estimated_cost=60.0),
                    Meal(type="dinner", name="晚餐", estimated_cost=80.0),
                ],
            )
        )

    return TripPlan(
        city="杭州",
        start_date="2026-08-10",
        days=days,
        weather=[
            WeatherInfo(date=d.date, day_weather="晴", day_temp=26.0, night_temp=17.0)
            for d in days
        ],
        budget=settle_budget(days, "中等"),
        overall_suggestions="建议",
        degraded=False,
    )


@pytest.fixture(autouse=True)
def clear_cache():
    """每个测试前清空缓存，避免用例间互相污染。"""
    from app.core.cache import cache

    cache.clear()
    yield
    cache.clear()