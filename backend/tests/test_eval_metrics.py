"""评测指标单元测试：纯函数，无网络依赖。"""

from app.models.schemas import Location
from eval.metrics import (
    check_attractions_per_day,
    check_budget_consistency,
    check_coordinates,
    check_days_count,
    check_meals_complete,
    check_no_hallucinated_poi,
    check_not_degraded,
    evaluate_plan,
)


def test_days_count_match(sample_plan, sample_request):
    ok, issues = check_days_count(sample_plan, sample_request)
    assert ok, issues


def test_days_count_mismatch(sample_plan, sample_request):
    sample_plan.days = sample_plan.days[:1]
    ok, issues = check_days_count(sample_plan, sample_request)
    assert not ok
    assert "2 天" in issues[0]


def test_meals_complete(sample_plan):
    ok, issues = check_meals_complete(sample_plan)
    assert ok, issues


def test_meals_missing_dinner(sample_plan):
    sample_plan.days[0].meals = [m for m in sample_plan.days[0].meals if m.type != "dinner"]
    ok, issues = check_meals_complete(sample_plan)
    assert not ok
    assert "dinner" in issues[0]


def test_coordinates_valid(sample_plan):
    ok, issues = check_coordinates(sample_plan)
    assert ok, issues


def test_coordinates_missing(sample_plan):
    sample_plan.days[0].attractions[0].location = None
    ok, issues = check_coordinates(sample_plan)
    assert not ok
    assert "无坐标" in issues[0]


def test_coordinates_zero_is_invalid(sample_plan):
    """(0,0) 通常意味着解析失败的默认值，应视为无效坐标。"""
    sample_plan.days[0].attractions[0].location = Location(longitude=0, latitude=0)
    ok, issues = check_coordinates(sample_plan)
    assert not ok
    assert "(0,0)" in issues[0]


def test_coordinates_out_of_range(sample_plan):
    sample_plan.days[0].attractions[0].location = Location(longitude=999, latitude=30)
    ok, _ = check_coordinates(sample_plan)
    assert not ok


def test_budget_consistency(sample_plan):
    ok, issues = check_budget_consistency(sample_plan)
    assert ok, issues


def test_budget_mismatch_detected(sample_plan):
    """预算是后端重算的，改总额应立即被指标发现——这是回归防线。"""
    sample_plan.budget.total = 99999.0
    ok, issues = check_budget_consistency(sample_plan)
    assert not ok
    assert "不一致" in issues[0]


def test_budget_zero_detected(sample_plan):
    sample_plan.budget.total = 0.0
    sample_plan.budget.total_attractions = 0.0
    sample_plan.budget.total_hotels = 0.0
    sample_plan.budget.total_meals = 0.0
    sample_plan.budget.total_transportation = 0.0
    ok, issues = check_budget_consistency(sample_plan)
    assert not ok


def test_no_hallucinated_poi(sample_plan):
    ok, issues = check_no_hallucinated_poi(sample_plan, ["景点1", "景点2"])
    assert ok, issues


def test_hallucinated_poi_detected(sample_plan):
    ok, issues = check_no_hallucinated_poi(sample_plan, ["西湖", "灵隐寺"])
    assert not ok
    assert "编造" in issues[0]


def test_hallucination_check_skipped_when_no_pool(sample_plan):
    """无候选池时跳过校验，而不是误报。"""
    ok, _ = check_no_hallucinated_poi(sample_plan, [])
    assert ok


def test_degraded_detected(sample_plan):
    sample_plan.degraded = True
    ok, issues = check_not_degraded(sample_plan)
    assert not ok
    assert "降级" in issues[0]


def test_fallback_content_detected(sample_plan):
    """即使 degraded 标记漏了，内容特征也能识别出兜底内容。"""
    sample_plan.days[0].description = "模板兜底行程（未使用 AI 生成）"
    ok, _ = check_not_degraded(sample_plan)
    assert not ok


def test_attractions_count_bounds(sample_plan):
    ok, _ = check_attractions_per_day(sample_plan)
    assert ok

    sample_plan.days[0].attractions = []
    ok, issues = check_attractions_per_day(sample_plan)
    assert not ok
    assert "无景点" in issues[0]


def test_evaluate_plan_full_pass(sample_plan, sample_request):
    report = evaluate_plan(sample_plan, sample_request, ["景点1", "景点2"])
    assert report["all_passed"], report["checks"]
    assert report["score"] == 1.0


def test_evaluate_plan_reports_each_check(sample_plan, sample_request):
    report = evaluate_plan(sample_plan, sample_request)
    assert len(report["checks"]) == 8
    assert "score" in report and "passed" in report and "total" in report