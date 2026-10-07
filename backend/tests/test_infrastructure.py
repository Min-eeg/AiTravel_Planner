"""基础设施测试：缓存、SSE 序列化、预算结算、LangGraph reducer。"""

import json

from app.agents.graph import _append_list, _merge_days, settle_budget
from app.core.cache import BaseCache, MemoryCache, make_cache_key
from app.core.events import sse
from app.models.schemas import DayPlan


# ========== 缓存 ==========


def test_memory_cache_roundtrip():
    cache = MemoryCache(default_ttl=60)
    cache.set("k", {"v": 1})
    assert cache.get("k") == {"v": 1}


def test_memory_cache_expiry():
    """TTL 到期后应返回 None，而不是返回过期值。"""
    cache = MemoryCache(default_ttl=0)
    cache.set("k", "v")
    assert cache.get("k") is None


def test_memory_cache_hit_rate():
    cache = MemoryCache(default_ttl=60)
    cache.set("k", "v")
    cache.get("k")  # hit
    cache.get("missing")  # miss
    assert cache.hits == 1
    assert cache.misses == 1
    assert cache.hit_rate == 0.5


def test_memory_cache_implements_interface():
    """可插拔缓存的关键：内存实现必须符合 BaseCache 契约。"""
    assert isinstance(MemoryCache(), BaseCache)


def test_cache_key_is_stable():
    """键生成必须与参数顺序无关，否则缓存永远不命中。"""
    a = make_cache_key("poi", keywords="西湖", city="杭州")
    b = make_cache_key("poi", city="杭州", keywords="西湖")
    assert a == b


def test_cache_key_differs_by_value():
    assert make_cache_key("poi", city="杭州") != make_cache_key("poi", city="上海")


# ========== SSE 序列化 ==========


def test_sse_frame_format():
    """SSE 帧必须以空行结尾，否则浏览器不认为帧结束。"""
    frame = sse("meta", {"city": "杭州"})
    assert frame.startswith("event: meta\n")
    assert frame.endswith("\n\n")


def test_sse_multiline_data():
    """JSON 序列化会把换行转义为字面 \\n，因此单行 data 即可安全承载换行内容。

    前端解析时用 join('\\n') 还原，多行 data 的情况也依然兼容。
    """
    frame = sse("day", {"desc": "line1\nline2"})
    data_lines = [l for l in frame.split("\n") if l.startswith("data: ")]
    assert len(data_lines) == 1

    body = data_lines[0][6:]
    assert json.loads(body)["desc"] == "line1\nline2"


def test_sse_roundtrip_json():
    payload = {"city": "杭州", "days": 3}
    frame = sse("meta", payload)
    body = "".join(
        l[6:] for l in frame.split("\n") if l.startswith("data: ")
    )
    assert json.loads(body) == payload


# ========== SSE 帧解析（服务端收集事件流用） ==========


def test_parse_sse_frames_basic():
    from app.api.routes.trip import parse_sse_frames

    frames = parse_sse_frames(sse("meta", {"city": "杭州"}))
    assert frames[0][0] == "meta"
    assert frames[0][1]["city"] == "杭州"


def test_parse_sse_frames_dispatch_by_event_name():
    """按 event 字段分派，而不是靠 payload 的 key。

    `done` 事件里有 days_generated（整数），若用 `'days' in payload` 判断会误命中。
    """
    from app.api.routes.trip import parse_sse_frames

    frames = parse_sse_frames(sse("done", {"days_generated": 3, "degraded": False}))
    event, payload = frames[0]
    assert event == "done"
    # 关键：payload 里没有 'days' 键，因此不能用 `in` 判断
    assert "days" not in payload


def test_parse_sse_frames_multiple():
    from app.api.routes.trip import parse_sse_frames

    combined = sse("meta", {"city": "杭州"}) + sse("done", {"days_generated": 2})
    frames = parse_sse_frames(combined)
    assert len(frames) == 2
    assert [f[0] for f in frames] == ["meta", "done"]


def test_parse_sse_frames_ignores_invalid_json():
    """坏帧应被跳过而不是抛异常——收集器不该因单帧失败而中断。"""
    from app.api.routes.trip import parse_sse_frames

    broken = "event: meta\ndata: {invalid json}\n\n"
    frames = parse_sse_frames(broken)
    assert frames == []


# ========== 预算结算 ==========


def _day(index: int, ticket: float) -> DayPlan:
    from app.models.schemas import Attraction, Location, Meal

    return DayPlan(
        date=f"2026-08-{10 + index:02d}",
        day_index=index,
        attractions=[
            Attraction(
                name="某景点",
                location=Location(longitude=120.1, latitude=30.2),
                ticket_price=ticket,
            )
        ],
        meals=[Meal(type="lunch", name="午餐", estimated_cost=60.0)],
    )


def test_budget_total_equals_sum_of_parts():
    """预算是后端重算的，总额必须严格等于各项之和。"""
    days = [_day(0, 60.0), _day(1, 40.0)]
    budget = settle_budget(days, "中等")
    parts = (
        budget.total_attractions
        + budget.total_hotels
        + budget.total_meals
        + budget.total_transportation
    )
    assert abs(budget.total - parts) < 0.01


def test_budget_scales_with_level():
    """宽松档位的住宿交通应明显高于经济档。"""
    days = [_day(0, 50.0)]
    economy = settle_budget(days, "经济")
    premium = settle_budget(days, "宽松")
    assert premium.total > economy.total


def test_budget_handles_empty_days():
    budget = settle_budget([], "中等")
    assert budget.total == 0.0



# ========== LangGraph reducer ==========


def test_merge_days_accumulates():
    """逐日生成时，每个节点只返回当天增量——reducer 必须累积而非覆盖。"""
    left = [DayPlan(date="2026-08-10", day_index=0)]
    right = [DayPlan(date="2026-08-11", day_index=1)]
    merged = _merge_days(left, right)
    assert len(merged) == 2
    assert [d.day_index for d in merged] == [0, 1]


def test_merge_days_overwrites_same_index():
    left = [DayPlan(date="2026-08-10", day_index=0, title="旧")]
    right = [DayPlan(date="2026-08-10", day_index=0, title="新")]
    merged = _merge_days(left, right)
    assert len(merged) == 1
    assert merged[0].title == "新"


def test_merge_days_with_empty():
    assert len(_merge_days([], [DayPlan(date="2026-08-10", day_index=0)])) == 1


def test_append_list_concatenates():
    assert _append_list(["a"], ["b", "c"]) == ["a", "b", "c"]


def test_append_list_with_none():
    assert _append_list(None, ["b"]) == ["b"]