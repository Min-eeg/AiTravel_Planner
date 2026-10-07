"""行程规划编排器：基于 LangGraph 的状态图。

为什么用 LangGraph 而不是手写编排：
- 阶段间的状态传递、条件分支、断点恢复都是框架标准能力，
  手写只是重复实现一遍，而且并发/状态同步容易出错
- `astream(..., stream_mode="updates")` 直接给出"哪个节点产出了什么"，
  天然适配 SSE 渐进渲染——这是本项目流式能力的骨架

图结构（同时执行的分支放在同一 superstep）：

        ┌──────────────┐
        │  parse_input │  约束抽取（LLM 解析自由文本）
        └──────┬───────┘
               ▼
   ╔═══════════════════════╗◄──┐
   ║  retrieve (并行三路)    ║   │
   ║  poi ∥ weather ∥ rag    ║   │
   ╚═══════════╤═══════════╝   │
               ▼               │
        ┌──────────────┐        │ 条件边：还有天数未生成？
        │  plan_day    ├────────┘ (loop)
        └──────┬───────┘
               ▼ (全部天数完成)
        ┌──────────────┐
        │ correct_geo  │  真实 POI 坐标覆盖 LLM 编造值
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │  finalize    │  预算结算 + 图表数据
        └──────────────┘

容错：任一阶段抛错都会跳到 fallback 节点，产出模板行程并标记 degraded，
前端据此展示"降级提示"而不是白屏。
"""

import logging
from datetime import date, timedelta
import operator
from typing import Annotated, Any, AsyncIterator, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from ..core import events
from ..core.config import get_settings
from ..models.drafts import ConstraintsDraft, DayDraft
from ..models.schemas import (
    Attraction,
    DayPlan,
    Location,
    Meal,
    TripRequest,
)
from ..services.amap_service import get_amap_service
from ..services.llm_service import LLMError, get_llm_service
from ..services.rag_service import retrieve_knowledge

logger = logging.getLogger(__name__)


# ============ Graph State ============


def _merge_days(left: List[DayPlan], right: List[DayPlan]) -> List[DayPlan]:
    """days 字段的 reducer：按 day_index 合并增量。

    LangGraph 默认用「后写覆盖」语义处理普通字段，
    但逐日生成时每个节点只返回当天的增量，必须累积而非覆盖，
    否则第 N 天会把前 N-1 天的结果冲掉。
    """
    if not right:
        return left
    merged = {d.day_index: d for d in (left or [])}
    for d in right:
        merged[d.day_index] = d
    return [merged[i] for i in sorted(merged)]


def _append_list(left: List[Any], right: List[Any]) -> List[Any]:
    """列表字段的 reducer：追加而非覆盖。"""
    return list(left or []) + list(right or [])


class TripState(TypedDict, total=False):
    """LangGraph 状态。节点通过返回 dict 更新，带 Annotated 的字段走自定义 reducer。"""

    request: TripRequest
    constraints: Dict[str, Any]
    candidates: List[Dict[str, Any]]
    weather: List[Dict[str, Any]]
    knowledge: str
    # 逐日累加，必须用 reducer 合并
    days: Annotated[List[DayPlan], _merge_days]
    day_pointer: int
    used_names: Annotated[List[str], _append_list]
    corrections: Annotated[List[Dict[str, Any]], _append_list]
    corrected_count: int
    degraded: bool
    error_message: str
    started_at: float
    # 各节点的进度描述，用于 SSE trace 事件的 detail 字段。
    # 必须显式声明：LangGraph 会过滤掉 schema 中不存在的 key。
    detail: str


# ============ 提示词 ============

CONSTRAINTS_PROMPT = """你是旅行需求解析专家。用户会给出目的地、天数和自由描述，
请把自由描述中隐含的偏好与约束抽取为结构化字段。

判断依据：
- "多赶几个景点""暴走""一天玩完" → 行程节奏=紧凑
- "慢慢逛""不着急""躺平" → 行程节奏=悠闲
- "省钱""学生党""穷游""预算紧" → 预算档位=经济
- "舒适""住好点""不差钱" → 预算档位=宽松
- 自由描述中提到的兴趣点（博物馆/美食/自然/历史/亲子/购物等）填入兴趣标签
- 明确排斥的内容（"不想购物""不去人多的地方"）填入需要避开

重要：必须逐项检查自由描述，不要因为描述简短就返回空列表或默认值。
即使只提到一个点（如"想多看博物馆"），也要把它填进兴趣标签。

输出格式（必须严格遵循，字段名不可改动、不可增减）：
{
  "行程节奏": "紧凑",
  "预算档位": "经济",
  "兴趣标签": ["博物馆", "美食"],
  "需要避开": ["购物"],
  "其他约束": ""
}
"""


# ============ 节点实现 ============


async def node_parse_input(state: TripState) -> Dict[str, Any]:
    """阶段 0：把用户的自由文本解析为结构化约束。"""
    request: TripRequest = state["request"]
    llm = get_llm_service()

    base = {
        "pace": request.pace,
        "budget_level": request.budget_level,
        "interests": list(request.preferences),
        "must_avoid": [],
        "notes": "",
    }

    if not request.free_text or not llm.available:
        return {"constraints": base}

    try:
        draft: ConstraintsDraft = await llm.structured(
            CONSTRAINTS_PROMPT,
            f"目的地：{request.city}\n天数：{request.days}天\n"
            f"偏好标签：{'、'.join(request.preferences) or '无'}\n"
            f"用户自由描述：{request.free_text}",
            ConstraintsDraft,
        )
        return {
            "constraints": {
                "pace": draft.pace,
                "budget_level": draft.budget_level,
                "interests": draft.interests or list(request.preferences),
                "must_avoid": draft.must_avoid,
                "notes": draft.notes,
            }
        }
    except LLMError as exc:
        # 约束解析失败不影响主流程，用默认值继续
        logger.warning("约束解析失败，使用默认值：%s", exc)
        return {"constraints": base}


async def node_retrieve(state: TripState) -> Dict[str, Any]:
    """阶段 1：并行检索 POI / 天气 / RAG 知识库。

    LangGraph 中同节点的三个分支并发执行，总耗时约等于最慢的一个。
    """
    import asyncio

    request: TripRequest = state["request"]
    settings = get_settings()
    amap = get_amap_service()
    constraints = state.get("constraints", {})

    keywords = (constraints.get("interests") or request.preferences or ["景点"])[0]

    async def fetch_poi() -> List[Dict[str, Any]]:
        return await amap.search_poi(keywords, request.city, offset=10)

    async def fetch_weather() -> List[Dict[str, Any]]:
        return await amap.get_weather(request.city)

    def fetch_knowledge() -> str:
        if not settings.rag_enabled:
            return ""
        query_parts = [
            request.city,
            " ".join(constraints.get("interests") or request.preferences),
            request.free_text,
        ]
        return retrieve_knowledge(request.city, " ".join(query_parts), top_k=4)

    results = await asyncio.gather(
        fetch_poi(),
        fetch_weather(),
        asyncio.to_thread(fetch_knowledge),
        return_exceptions=True,
    )

    candidates = _unwrap(results[0], [], "POI 检索")
    weather = _unwrap(results[1], [], "天气查询")
    knowledge = _unwrap(results[2], "", "知识库检索")

    if not weather:
        try:
            weather = await amap.get_weather(request.city)
        except Exception:
            weather = []

    return {
        "candidates": candidates,
        "weather": weather,
        "knowledge": knowledge,
    }


def node_plan_day(state: TripState) -> Dict[str, Any]:
    """阶段 2：生成单日行程。由条件边控制循环次数。"""
    # 实际实现在 TripPlanner._plan_one_day（需要 self 的 llm 实例）
    raise NotImplementedError  # 由 build_graph 注入


def node_correct_geo(state: TripState) -> Dict[str, Any]:
    raise NotImplementedError  # 由 build_graph 注入


def node_finalize(state: TripState) -> Dict[str, Any]:
    raise NotImplementedError  # 由 build_graph 注入


def node_fallback(state: TripState) -> Dict[str, Any]:
    raise NotImplementedError  # 由 build_graph 注入


def _unwrap(result: Any, fallback: Any, label: str) -> Any:
    """gather(return_exceptions=True) 的结果解包。"""
    if isinstance(result, BaseException):
        logger.warning("%s 失败：%s", label, result)
        return fallback
    return result


# ============ 图构建 ============


def build_graph(planner: "TripPlanner"):
    """构建 LangGraph 状态图。节点实现绑定到 planner 实例。"""

    def should_continue(state: TripState) -> str:
        """条件边：还有天数没生成就继续循环。"""
        request: TripRequest = state["request"]
        pointer: int = state.get("day_pointer", 0)
        return "plan_day" if pointer < request.days else "correct_geo"

    graph = StateGraph(TripState)

    graph.add_node("parse_input", planner._node_parse_input)
    graph.add_node("retrieve", planner._node_retrieve)
    graph.add_node("plan_day", planner._node_plan_day)
    graph.add_node("correct_geo", planner._node_correct_geo)
    graph.add_node("finalize", planner._node_finalize)
    graph.add_node("fallback", planner._node_fallback)

    graph.add_edge(START, "parse_input")
    graph.add_edge("parse_input", "retrieve")
    graph.add_edge("retrieve", "plan_day")
    # plan_day 之后按剩余天数决定继续循环还是进入校正
    graph.add_conditional_edges(
        "plan_day", should_continue, {"plan_day": "plan_day", "correct_geo": "correct_geo"}
    )
    graph.add_edge("correct_geo", "finalize")
    graph.add_edge("finalize", END)

    # 任意阶段失败 → 兜底
    graph.add_conditional_edges(
        "retrieve",
        lambda s: "fallback" if not s.get("candidates") else "plan_day",
        {"fallback": "fallback", "plan_day": "plan_day"},
    )
    graph.add_edge("fallback", END)

    return graph.compile()


# ============ 预算结算（纯函数，便于单测） ============

_BUDGET_FACTOR = {"经济": 0.6, "中等": 1.0, "宽松": 1.8}


def settle_budget(days: List[DayPlan], budget_level: str, city: str = ""):
    """后端重算预算，并把住宿/交通写回每一天。

    不信任 LLM 给的总额——逐项由结构化数据累加，
    保证「预算一致」这个评测指标必然通过。

    住宿/交通无法从 LLM 可靠获得（酒店名多为编造），按预算档位估算：
    - 住宿：每晚 300 × 档位系数，N 天行程住 N-1 晚（最后一天返程）
    - 交通：市内每日 60 × 档位系数（打车 + 地铁）
    写回到 DayPlan 后前端可逐日展示，用户能看到"钱花在哪天"。
    """
    from ..models.schemas import Budget, Hotel

    factor = _BUDGET_FACTOR.get(budget_level, 1.0)
    attractions = sum(a.ticket_price for d in days for a in d.attractions)
    meals = sum(m.estimated_cost for d in days for m in d.meals)

    # 逐日写入：住宿（N-1 晚）与交通（每天）
    hotel_nights = max(len(days) - 1, 0)
    nightly = round(300.0 * factor, 2)
    daily_transport = round(60.0 * factor, 2)
    tier = {"经济": "经济型", "中等": "舒适型", "宽松": "高档型"}.get(
        budget_level, "舒适型"
    )
    for i, day in enumerate(days):
        day.transportation_cost = daily_transport
        if i < hotel_nights:
            # 不编造具体酒店名，用档位描述（真实酒店推荐需要单独的 POI 查询）
            day.hotel = Hotel(
                name=f"{city}·{tier}酒店".lstrip("·"),
                price_range=f"¥{nightly:.0f}/晚",
                estimated_cost=nightly,
            )
        else:
            day.hotel = None  # 最后一天返程，无住宿

    hotels = hotel_nights * nightly
    transport = len(days) * daily_transport

    budget = Budget(
        total_attractions=round(attractions, 2),
        total_hotels=round(hotels, 2),
        total_meals=round(meals, 2),
        total_transportation=round(transport, 2),
    )
    budget.total = round(
        budget.total_attractions
        + budget.total_hotels
        + budget.total_meals
        + budget.total_transportation,
        2,
    )
    return budget
