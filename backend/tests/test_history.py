"""行程历史持久化测试。

用临时 SQLite 文件，不污染开发库。
"""

from pathlib import Path

import pytest

from app.db.history import TripHistoryService


@pytest.fixture
def service(tmp_path: Path, monkeypatch):
    """每个用例用独立的临时数据库文件。"""
    db = tmp_path / "test_trip.db"
    monkeypatch.setattr("app.db.history.DB_PATH", db)
    return TripHistoryService()


def _plan(city="杭州", days=2, budget=757, degraded=False):
    return {
        "city": city,
        "start_date": "2026-08-10",
        "days": [
            {
                "date": f"2026-08-{10 + i}",
                "day_index": i,
                "attractions": [{"name": f"景点{i}", "ticket_price": 60}],
            }
            for i in range(days)
        ],
        "budget": {"total": budget},
        "degraded": degraded,
    }


# ========== 保存 ==========


def test_save_and_list(service):
    result = service.save(_plan(), ["历史文化"])
    assert result["duplicate"] is False

    items = service.list()
    assert len(items) == 1
    assert items[0]["city"] == "杭州"
    assert items[0]["days_count"] == 2
    assert items[0]["preferences"] == ["历史文化"]


def test_duplicate_detected(service):
    """相同内容重复保存应复用已有记录，不产生重复行。"""
    plan = _plan()
    first = service.save(plan)
    second = service.save(plan)

    assert second["duplicate"] is True
    assert second["trip_id"] == first["trip_id"]
    assert len(service.list()) == 1


def test_different_content_not_duplicate(service):
    a = service.save(_plan(city="杭州"))
    b = service.save(_plan(city="北京"))

    assert b["duplicate"] is False
    assert a["trip_id"] != b["trip_id"]
    assert len(service.list()) == 2


def test_hash_stable_across_key_order():
    """字典键顺序不同但内容相同，哈希应一致（否则去重会失效）。"""
    from app.db.history import TripHistoryService as S

    a = {"city": "杭州", "days": [1, 2]}
    b = {"days": [1, 2], "city": "杭州"}
    assert S._hash_plan(a) == S._hash_plan(b)


def test_hash_differs_for_different_content():
    from app.db.history import TripHistoryService as S

    assert S._hash_plan({"a": 1}) != S._hash_plan({"a": 2})


# ========== 详情与删除 ==========


def test_detail_roundtrip(service):
    plan = _plan()
    trip_id = service.save(plan)["trip_id"]

    detail = service.detail(trip_id)
    assert detail is not None
    assert detail["summary"]["city"] == "杭州"
    # 完整行程应原样还原
    assert detail["plan"] == plan


def test_detail_missing_returns_none(service):
    assert service.detail(9999) is None


def test_delete(service):
    trip_id = service.save(_plan())["trip_id"]
    assert service.delete(trip_id) is True
    assert service.list() == []


def test_delete_missing_returns_false(service):
    assert service.delete(9999) is False


# ========== 数据完整性 ==========


def test_degraded_flag_persisted(service):
    trip_id = service.save(_plan(degraded=True))["trip_id"]
    detail = service.detail(trip_id)
    assert detail["summary"]["degraded"] is True


def test_budget_persisted_as_int(service):
    """预算是 int 列，浮点 757.0 应存成 757。"""
    trip_id = service.save(_plan(budget=757.9))["trip_id"]
    detail = service.detail(trip_id)
    assert isinstance(detail["summary"]["total_budget"], int)


def test_list_is_newest_first(service):
    """历史列表应按时间倒序，最近保存的在最前。"""
    service.save(_plan(city="杭州"))
    service.save(_plan(city="北京"))

    items = service.list()
    assert items[0]["city"] == "北京"


def test_list_respects_limit(service):
    for i in range(5):
        service.save(_plan(city=f"城市{i}"))
    assert len(service.list(limit=3)) == 3


def test_json_with_chinese_roundtrip(service):
    """中文内容必须正确存储与还原，不能有编码问题。"""
    plan = _plan()
    plan["days"][0]["attractions"][0]["name"] = "南宋德寿宫遗址博物馆"
    trip_id = service.save(plan)["trip_id"]

    detail = service.detail(trip_id)
    name = detail["plan"]["days"][0]["attractions"][0]["name"]
    assert name == "南宋德寿宫遗址博物馆"


def test_nested_structure_preserved(service):
    """嵌套结构（多层 dict / list）应完整还原。"""
    plan = _plan()
    plan["days"][0]["hotel"] = {
        "name": "湖滨客栈",
        "location": {"longitude": 120.15, "latitude": 30.25},
    }
    trip_id = service.save(plan)["trip_id"]

    detail = service.detail(trip_id)
    hotel = detail["plan"]["days"][0]["hotel"]
    assert hotel["location"]["longitude"] == 120.15