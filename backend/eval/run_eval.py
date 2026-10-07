"""评测运行器。

用法：
    python -m eval.run_eval --mode mock              # 免 Key 自检（模板兜底跑通管线）
    python -m eval.run_eval --mode real              # 真实调用 Agent，输出质量报告
    python -m eval.run_eval --mode real --limit 3    # 只跑前 3 条
    python -m eval.run_eval --mode real --no-rag     # 关闭知识库注入，做 A/B 对比

设计要点：
- mock 模式用模板兜底链路跑完整管线，无需任何 API Key，
  适合 CI 与本地开发自检
- real 模式走完整 LangGraph 编排，输出各指标通过率
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# 保证从 backend 目录直接运行脚本也能导入 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.graph import settle_budget  # noqa: E402
from app.models.schemas import DayPlan, TripPlan, TripRequest, WeatherInfo  # noqa: E402
from app.models.schemas import Budget  # noqa: E402
from eval.cases import EVAL_CASES  # noqa: E402
from eval.metrics import evaluate_plan, format_report  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def build_mock_plan(request: TripRequest) -> TripPlan:
    """构造一份「结构完整但内容为模板」的行程。

    用于验证：即使 AI 完全不可用，评测管线本身能否跑通并给出合理评分。
    """
    from datetime import date, timedelta

    from app.models.schemas import Attraction, Location, Meal, WeatherInfo

    base = date.fromisoformat(request.start_date)
    days = []
    for i in range(request.days):
        day_date = (base + timedelta(days=i)).isoformat()
        days.append(
            DayPlan(
                date=day_date,
                day_index=i,
                title=f"第{i + 1}天",
                description="模板兜底行程",
                attractions=[
                    Attraction(
                        name=f"{request.city}景点{j + 1}",
                        location=Location(longitude=120.1 + i * 0.01, latitude=30.2 + j * 0.01),
                        ticket_price=50.0,
                    )
                    for j in range(2)
                ],
                meals=[
                    Meal(type="breakfast", name="早餐", estimated_cost=30.0),
                    Meal(type="lunch", name="午餐", estimated_cost=60.0),
                    Meal(type="dinner", name="晚餐", estimated_cost=80.0),
                ],
            )
        )

    weather = [
        {
            "date": d.date,
            "day_weather": "晴",
            "night_weather": "多云",
            "day_temp": 26.0,
            "night_temp": 17.0,
        }
        for d in days
    ]

    return TripPlan(
        city=request.city,
        start_date=request.start_date,
        days=days,
        weather=[WeatherInfo(**w) for w in weather],
        budget=settle_budget(days, request.budget_level),
        overall_suggestions="模板建议",
        degraded=True,
    )


async def run_real_case(case: Dict[str, Any]) -> TripPlan:
    """真实调用 LangGraph 编排，收集 SSE 事件拼装成完整行程。

    必须按 event 字段分派，不能靠 payload 里有没有某个 key：
    `done` 事件的 `days_generated` 会让 `'days' in payload` 误判为True。
    """
    from app.agents.planner import get_trip_planner
    from app.api.routes.trip import parse_sse_frames

    request = TripRequest(**case["request"])
    planner = get_trip_planner()

    plan = TripPlan(
        city=request.city, start_date=request.start_date, days=[], degraded=False
    )

    async for chunk in planner.stream_plan(request):
        for event, payload in parse_sse_frames(chunk):
            if event == "day":
                plan.days.append(DayPlan(**payload["day"]))
            elif event == "chart":
                # 走 SSE 得到的是 dict，指标里按属性访问，必须转成模型对象
                plan.budget = Budget(**payload["budget"])
                plan.weather = [WeatherInfo(**w) for w in payload.get("weather", [])]
            elif event == "done":
                plan.degraded = bool(payload.get("degraded", False))

    return plan


async def run_case(case: Dict[str, Any], mode: str) -> Dict[str, Any]:
    request = TripRequest(**case["request"])

    if mode == "mock":
        plan = build_mock_plan(request)
        # mock 模式没有候选池，跳过「无编造景点」校验
        valid_names: List[str] = []
    else:
        plan = await run_real_case(case)
        from app.services.amap_service import get_amap_service

        keywords = (request.preferences or ["景点"])[0]
        pois = await get_amap_service().search_poi(keywords, request.city, offset=10)
        valid_names = [p["name"] for p in pois]

    return evaluate_plan(plan, request, valid_names)


async def main_async(args: argparse.Namespace) -> int:
    if args.no_rag:
        os.environ["RAG_ENABLED"] = "0"
        logger.info("已关闭知识库注入（RAG_ENABLED=0）")

    cases = EVAL_CASES[: args.limit] if args.limit else EVAL_CASES

    logger.info("开始评测：mode=%s，用例数=%s", args.mode, len(cases))
    logger.info("=" * 62)

    reports = []
    for case in cases:
        try:
            report = await run_case(case, args.mode)
        except Exception as exc:
            logger.error("用例「%s」执行异常：%s", case["name"], exc)
            report = {
                "total": 1, "passed": 0, "score": 0.0, "all_passed": False,
                "checks": {"执行": {"passed": False, "issues": [str(exc)[:200]]}},
                "warnings": [],
            }
        reports.append(report)
        print(format_report(report, case["name"]))
        print("-" * 62)

    total = sum(r["total"] for r in reports)
    passed = sum(r["passed"] for r in reports)
    avg = round(sum(r["score"] for r in reports) / len(reports), 3) if reports else 0.0

    print("=" * 62)
    print(f"汇总：{passed}/{total} 项通过，平均得分 {avg}")
    print(f"全通过用例：{sum(1 for r in reports if r['all_passed'])}/{len(reports)}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写入 {args.json}")

    return 0 if avg >= args.threshold else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="行程生成质量评测")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条用例")
    parser.add_argument("--no-rag", action="store_true", help="关闭知识库注入做 A/B")
    parser.add_argument("--json", default="", help="将报告写入 JSON 文件")
    parser.add_argument("--threshold", type=float, default=0.6, help="平均分阈值，低于则退出码非 0")
    args = parser.parse_args()

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())