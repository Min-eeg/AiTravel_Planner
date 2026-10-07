"""结构化输出 schema 的回归测试。

这些用例锁定的都是真实踩过的坑——中文模型的结构化输出偏差，
一旦回归会导致整份行程被静默降级为模板，而测试很难发现。
"""

import pytest

from app.models.drafts import AttractionDraft, ConstraintsDraft, DayDraft, MealDraft


# ========== schema 字段名必须是中文 ==========


def test_day_draft_schema_uses_chinese_keys():
    """模型按 schema 里的 property 名输出，字段名必须是中文。

    实测：schema 用英文名时，qwen-max 会输出 {'景点': ..., '餐饮': ...} 这类
    自造中文键，导致校验失败、整份内容被丢弃降级。
    """
    props = DayDraft.model_json_schema()["properties"]
    assert set(props.keys()) == {"当天主题", "当天概述", "景点安排", "餐饮安排"}


def test_attraction_schema_uses_chinese_keys():
    props = AttractionDraft.model_json_schema()["properties"]
    assert "景点名称" in props
    assert "游览分钟" in props


def test_constraints_schema_uses_chinese_keys():
    props = ConstraintsDraft.model_json_schema()["properties"]
    assert set(props.keys()) == {"行程节奏", "预算档位", "兴趣标签", "需要避开", "其他约束"}


# ========== 中文键解析 ==========


def test_parse_chinese_keys():
    draft = DayDraft(**{
        "当天主题": "西湖环线",
        "当天概述": "以西湖为核心",
        "景点安排": [
            {"景点名称": "西湖", "游览分钟": 90, "看点介绍": "漫步", "类别": "自然"},
        ],
        "餐饮安排": [
            {"餐次": "breakfast", "餐名": "片儿川", "预估人均消费": 25},
            {"餐次": "lunch", "餐名": "楼外楼", "预估人均消费": 120},
            {"餐次": "dinner", "餐名": "夜市", "预估人均消费": 60},
        ],
    })
    assert draft.title == "西湖环线"
    assert draft.attractions[0].name == "西湖"
    assert draft.attractions[0].visit_duration == 90
    assert len(draft.normalized_meals()) == 3


def test_parse_english_keys_still_works():
    """业务代码用英文属性名访问，populate_by_name 保证英文键也能填充。"""
    draft = DayDraft(**{
        "title": "T",
        "attractions": [{"name": "A"}],
        "meals": [{"type": "lunch"}],
    })
    assert draft.title == "T"
    assert draft.attractions[0].name == "A"


# ========== 餐饮两种形态 ==========


def test_meals_as_array():
    draft = DayDraft(**{
        "景点安排": [{"景点名称": "西湖"}],
        "餐饮安排": [
            {"餐次": "breakfast", "餐名": "A"},
            {"餐次": "lunch", "餐名": "B"},
            {"餐次": "dinner", "餐名": "C"},
        ],
    })
    assert len(draft.normalized_meals()) == 3


def test_meals_as_object_of_strings():
    """模型误把三餐当对象输出——必须能还原。"""
    draft = DayDraft(**{
        "景点安排": [{"景点名称": "西湖"}],
        "餐饮安排": {"breakfast": "知味观", "lunch": "楼外楼", "dinner": "外婆家"},
    })
    meals = draft.normalized_meals()
    assert len(meals) == 3
    assert {m.type for m in meals} == {"breakfast", "lunch", "dinner"}
    assert any("知味观" in m.name for m in meals)


def test_meals_as_object_of_dicts():
    """对象里嵌 dict，且值可能是中英文混合键。"""
    draft = DayDraft(**{
        "景点安排": [{"景点名称": "西湖"}],
        "餐饮安排": {
            "breakfast": {"餐名": "豆浆油条", "预估人均消费": 12},
            "lunch": {"name": "Toast", "estimated_cost": 10},
        },
    })
    meals = draft.normalized_meals()
    assert len(meals) == 2
    assert {m.type for m in meals} == {"breakfast", "lunch"}


def test_meals_partial_missing():
    """部分缺失不应抛异常，补齐逻辑会兜住。"""
    draft = DayDraft(**{
        "景点安排": [{"景点名称": "西湖"}],
        "餐饮安排": {"lunch": "面馆"},
    })
    assert len(draft.normalized_meals()) == 1


def test_meals_empty_object():
    draft = DayDraft(**{"景点安排": [{"景点名称": "西湖"}], "餐饮安排": {}})
    assert draft.normalized_meals() == []


# ========== 额外字段应被忽略 ==========


def test_extra_fields_ignored():
    """模型模仿提示词格式多返回「城市」「日期」等字段，不应导致校验失败。"""
    draft = DayDraft(**{
        "城市": "杭州",  # 多余
        "日期": "2026-08-10",  # 多余
        "当天主题": "西湖环线",
        "景点安排": [{"景点名称": "西湖", "额外说明": "忽略我"}],
        "餐饮安排": [{"餐次": "lunch", "餐名": "面馆"}],
    })
    assert draft.title == "西湖环线"
    assert draft.attractions[0].name == "西湖"


# ========== 单位说明必须存在 ==========


def test_duration_field_documents_unit():
    """单位不写清，模型会把「2 小时」填进分钟字段。"""
    desc = AttractionDraft.model_json_schema()["properties"]["游览分钟"]["description"]
    assert "分钟" in desc


def test_cost_field_documents_unit():
    desc = MealDraft.model_json_schema()["properties"]["预估人均消费"]["description"]
    assert "元" in desc


# ========== DashScope json_object 约束 ==========


def test_json_hint_added_when_missing():
    from app.services.llm_service import _ensure_json_hint

    result = _ensure_json_hint("帮我规划行程")
    assert "json" in result.lower()


def test_json_hint_not_duplicated():
    from app.services.llm_service import _ensure_json_hint

    original = "请输出 json"
    assert _ensure_json_hint(original) == original


@pytest.mark.parametrize(
    "text",
    ["纯json", "JSON格式", "Json object"],
)
def test_json_hint_detects_any_case(text):
    from app.services.llm_service import _ensure_json_hint

    assert _ensure_json_hint(text) == text


# ========== 错误分类 ==========


def test_classify_arrearage_as_unavailable():
    """欠费是账户级问题，重试无意义，必须立刻上抛。"""
    from app.services.llm_service import LLMUnavailableError, _classify_error

    err = _classify_error("Error code: 400 - {'error': {'type': 'Arrearage'}}")
    assert isinstance(err, LLMUnavailableError)
    assert "欠费" in str(err)


def test_classify_invalid_key_as_unavailable():
    from app.services.llm_service import LLMUnavailableError, _classify_error

    err = _classify_error("invalid api-key provided")
    assert isinstance(err, LLMUnavailableError)


def test_classify_timeout_as_retryable():
    """超时是瞬时问题，应归为可重试。"""
    from app.services.llm_service import LLMError, LLMUnavailableError, _classify_error

    err = _classify_error("Request timed out")
    assert isinstance(err, LLMError)
    assert not isinstance(err, LLMUnavailableError)


def test_classify_5xx_as_retryable():
    from app.services.llm_service import LLMError, LLMUnavailableError, _classify_error

    err = _classify_error("Internal Server Error 503")
    assert isinstance(err, LLMError)
    assert not isinstance(err, LLMUnavailableError)


# ========== 数值合理性兜底 ==========


def test_sane_duration_converts_hours():
    """模型把「2 小时」填成 2，应换算为 120 分钟。"""
    from app.agents.planner import _sane_duration

    assert _sane_duration(2) == 120
    assert _sane_duration(3) == 180


def test_sane_duration_keeps_valid():
    from app.agents.planner import _sane_duration

    assert _sane_duration(90) == 90
    assert _sane_duration(120) == 120


def test_sane_duration_handles_none_and_caps():
    from app.agents.planner import _sane_duration

    assert _sane_duration(None) == 120
    assert _sane_duration(0) == 120
    assert _sane_duration(99999) == 480  # 上限 8 小时


def test_sane_cost_bounds():
    from app.agents.planner import _sane_cost

    assert _sane_cost(None) == 50.0
    assert _sane_cost(1) == 20.0     # 过低给下限
    assert _sane_cost(100) == 100.0  # 正常值保留
    assert _sane_cost(99999) == 500.0  # 过高截断