"""行程质量评测指标（纯函数，无网络依赖，可独立单元测试）。

设计原则：指标不针对单条用例写死断言，而是独立的校验函数。
这样可以整体评估 Agent 在各类输入下的表现，也便于新增用例。

指标分两类：
- 硬指标：结构完整性（三餐/坐标/天数），不满足即质量问题
- 软指标：内容合理性（预算一致、非降级），可通过降级兜底绕过的要显式标记
"""

from typing import Any, Dict, List, Tuple

from app.models.schemas import DayPlan, TripPlan, TripRequest

CheckResult = Tuple[bool, List[str]]


def check_days_count(plan: TripPlan, request: TripRequest) -> CheckResult:
    """天数与请求一致。"""
    actual = len(plan.days)
    expected = request.days
    if actual == expected:
        return True, []
    return False, [f"期望 {expected} 天，实际 {actual} 天"]


def check_attractions_per_day(plan: TripPlan) -> CheckResult:
    """每天至少 1 个景点，最多 4 个（过多会导致行程不现实）。"""
    issues = []
    for day in plan.days:
        n = len(day.attractions)
        if n == 0:
            issues.append(f"第{day.day_index + 1}天无景点")
        elif n > 4:
            issues.append(f"第{day.day_index + 1}天景点过多({n}个)")
    return (not issues), issues


def check_meals_complete(plan: TripPlan) -> CheckResult:
    """每天必须含早/中/晚三餐。"""
    issues = []
    required = {"breakfast", "lunch", "dinner"}
    for day in plan.days:
        types = {m.type for m in day.meals}
        missing = required - types
        if missing:
            issues.append(
                f"第{day.day_index + 1}天缺餐: {', '.join(sorted(missing))}"
            )
    return (not issues), issues


def check_weather_coverage(plan: TripPlan) -> CheckResult:
    """天气数据应覆盖行程的天数。

    注意：只比对「数量」而非「日期完全一致」——
    天气数据源返回的是从今天起的未来 N 天，而行程可能指定了其他起始日期
    （例如评测用例固定用2026-08-10 这样的历史日期），
    此时日期字符串必然对不上，但数据本身是有效的。
    因此这里只校验天数是否够用。
    """
    needed = len(plan.days)
    got = len(plan.weather)
    if got == 0:
        return True, ["无天气数据，图表将不显示天气曲线（非致命）"]
    if got < needed:
        return True, [f"天气数据 {got} 天，少于行程 {needed} 天（部分日期无数据，非致命）"]
    return True, []


def check_budget_consistency(plan: TripPlan) -> CheckResult:
    """预算总额 = 各分项之和。

    后端用 settle_budget 强制重算，因此这条应当恒通过；
    它是一道"回归防线"——若将来改成让 LLM 自己算，这里会先报警。
    """
    b = plan.budget
    if b is None:
        return False, ["缺少预算"]
    parts_sum = (
        b.total_attractions + b.total_hotels + b.total_meals + b.total_transportation
    )
    if abs(b.total - parts_sum) > 0.01:
        return False, [f"预算不一致: total={b.total}, 各部分和={parts_sum}"]
    if b.total <= 0:
        return False, ["预算总额为 0（未计算）"]
    return True, []


def check_coordinates(plan: TripPlan) -> CheckResult:
    """景点坐标必须存在且落在合法经纬度范围内。

    这条是硬指标：坐标缺失会直接导致地图无法渲染。
    """
    issues = []
    for day in plan.days:
        for a in day.attractions:
            if a.location is None:
                issues.append(f"景点'{a.name}'无坐标")
                continue
            lng, lat = a.location.longitude, a.location.latitude
            if not (-180 <= lng <= 180 and -90 <= lat <= 90):
                issues.append(f"景点'{a.name}'坐标越界: {lng},{lat}")
            elif lng == 0 and lat == 0:
                # (0,0) 是无效默认值，通常意味着解析失败
                issues.append(f"景点'{a.name}'坐标为(0,0)，疑似无效值")
    return (not issues), issues


def check_no_hallucinated_poi(plan: TripPlan, valid_names: List[str]) -> CheckResult:
    """景点必须来自候选池，杜绝 LLM 编造。

    valid_names 为检索阶段拿到的 POI 名称集合。
    """
    if not valid_names:
        return True, []  # 无候选可校验时跳过
    allowed = set(valid_names)
    issues = []
    for day in plan.days:
        for a in day.attractions:
            if a.name not in allowed:
                issues.append(f"景点'{a.name}'不在候选列表中（疑似编造）")
    return (not issues), issues


def check_no_empty_fields(plan: TripPlan) -> CheckResult:
    """关键字段非空。"""
    issues = []
    if not plan.city:
        issues.append("城市为空")
    for day in plan.days:
        if not day.attractions:
            continue  # 已由 check_attractions_per_day 报告
        if not day.title and not day.description:
            issues.append(f"第{day.day_index + 1}天标题与描述均为空")
    return (not issues), issues


def check_not_degraded(plan: TripPlan) -> CheckResult:
    """检测是否走了降级兜底。

    降级不是 bug（保证可用性），但必须被显式标记——
    评测时要能区分"AI 生成的行程"和"模板兜底行程"。
    """
    if plan.degraded:
        return False, ["该行程由降级兜底链路生成"]
    for day in plan.days:
        if day.description and "模板兜底" in day.description:
            return False, [f"第{day.day_index + 1}天为模板兜底内容"]
    return True, []


# ============ 汇总 ============


def evaluate_plan(
    plan: TripPlan, request: TripRequest, valid_names: List[str] | None = None
) -> Dict[str, Any]:
    """对一份行程做全量指标评估，返回报告。"""
    checks: Dict[str, CheckResult] = {
        "天数匹配": check_days_count(plan, request),
        "景点数量": check_attractions_per_day(plan),
        "三餐完整": check_meals_complete(plan),
        "预算一致": check_budget_consistency(plan),
        "坐标有效": check_coordinates(plan),
        "无编造景点": check_no_hallucinated_poi(plan, valid_names or []),
        "无空字段": check_no_empty_fields(plan),
        "非降级行程": check_not_degraded(plan),
    }

    # 天气覆盖作为软指标单独记录，不计入硬性得分
    soft_issues = check_weather_coverage(plan)[1]

    passed = sum(1 for _, (ok, _) in checks.items() if ok)
    total = len(checks)

    return {
        "total": total,
        "passed": passed,
        "score": round(passed / total, 3),
        "all_passed": passed == total,
        "checks": {
            name: {"passed": ok, "issues": issues}
            for name, (ok, issues) in checks.items()
        },
        "warnings": soft_issues,
    }


def format_report(report: Dict[str, Any], case_name: str = "") -> str:
    """把报告格式化为可读文本。"""
    lines = []
    if case_name:
        lines.append(f"【{case_name}】")

    score = report["score"]
    status = "✓" if report["all_passed"] else "✗"
    lines.append(f"  得分 {report['passed']}/{report['total']} ({score}) {status}")

    for name, result in report["checks"].items():
        mark = "✓" if result["passed"] else "✗"
        lines.append(f"    {mark} {name}")
        for issue in result["issues"]:
            lines.append(f"       - {issue}")

    if report.get("warnings"):
        lines.append("  警告：")
        for w in report["warnings"]:
            lines.append(f"    ! {w}")

    return "\n".join(lines)