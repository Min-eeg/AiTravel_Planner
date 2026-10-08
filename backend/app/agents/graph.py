"""行程规划编排器：基于 LangGraph 的**多智能体**状态图。

为什么用 LangGraph 而不是手写编排：
- 阶段间的状态传递、条件分支、断点恢复都是框架标准能力，
  手写只是重复实现一遍，而且并发/状态同步容易出错
- `astream(..., stream_mode="updates")` 直接给出"哪个节点产出了什么"，
  天然适配 SSE 渐进渲染——这是本项目流式能力的骨架

多 Agent 分工（每个 Agent 是图中的一个节点，职责单一、可独立替换）：

        ┌──────────────┐
        │  parse_input │  约束 Agent（LLM 解析自由文本）
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │  scout_poi   │  景点侦察 Agent：LLM 生成搜索计划 → MCP 工具执行
        └──────┬───────┘
               ▼
   ╔═══════════════════════╗
   ║  retrieve (并行两路)    ║   天气 ∥ RAG 知识库
   ╚═══════════╤═══════════╝
               ▼
        ┌──────────────┐
        │  plan_day    │  行程规划 Agent（逐日循环）
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │  review_day  │  行程评审 Agent：质检不合格打回 plan_day ⟲
        └──────┬───────┘  （每天最多打回 1 次）
               ▼ (全部天数通过评审)
        ┌──────────────┐
        │ correct_geo  │  真实 POI 坐标覆盖 LLM 编造值
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │  finalize    │  预算结算 + 图表数据
        └──────────────┘

容错：候选检索失败跳 fallback 节点，产出模板行程并标记 degraded，
前端据此展示"降级提示"而不是白屏。
"""

import logging
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from ..models.schemas import DayPlan, TripRequest

if TYPE_CHECKING:
    from .planner import TripPlanner

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
    # —— Scout Agent 产物 ——
    search_plan: List[str]  # LLM 决策的搜索关键词（检索计划）
    candidates: List[Dict[str, Any]]
    weather: List[Dict[str, Any]]
    knowledge: str
    # —— Reviewer Agent 产物 ——
    review_verdict: str  # accept / reject（当天评审结论）
    review_feedback: str  # 打回时给规划 Agent 的修改意见
    day_retries: Dict[str, int]  # 每天已被打回的次数（key=str(day_index)）
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
#
# 各节点的真实实现绑定在 TripPlanner 实例上（见 planner.py），
# build_graph 负责把它们装配进状态图。


# ============ 图构建 ============

# 评审打回的重试上限：超过后放行当天的结果，避免死循环拖垮整体耗时
MAX_DAY_RETRIES = 1


def build_graph(planner: "TripPlanner"):
    """构建 LangGraph 状态图。节点实现绑定到 planner 实例。"""

    def after_review(state: TripState) -> str:
        """条件边：评审打回则重做当天；否则按剩余天数决定继续循环还是收尾。

        打回时 review_day 节点已把 day_pointer 指回当天，
        plan_day 会带着 review_feedback 重新生成同一天。
        """
        request: TripRequest = state["request"]
        if state.get("review_verdict") == "reject":
            return "plan_day"
        pointer: int = state.get("day_pointer", 0)
        return "plan_day" if pointer < request.days else "correct_geo"

    graph = StateGraph(TripState)

    graph.add_node("parse_input", planner._node_parse_input)
    graph.add_node("scout_poi", planner._node_scout_poi)
    graph.add_node("retrieve", planner._node_retrieve)
    graph.add_node("plan_day", planner._node_plan_day)
    graph.add_node("review_day", planner._node_review_day)
    graph.add_node("correct_geo", planner._node_correct_geo)
    graph.add_node("finalize", planner._node_finalize)
    graph.add_node("fallback", planner._node_fallback)

    graph.add_edge(START, "parse_input")
    graph.add_edge("parse_input", "scout_poi")
    graph.add_edge("scout_poi", "retrieve")
    graph.add_edge("retrieve", "plan_day")
    # 每天生成后先过评审 Agent，再决定打回 / 继续下一天 / 收尾
    graph.add_edge("plan_day", "review_day")
    graph.add_conditional_edges(
        "review_day",
        after_review,
        {"plan_day": "plan_day", "correct_geo": "correct_geo"},
    )
    graph.add_edge("correct_geo", "finalize")
    graph.add_edge("finalize", END)

    # 侦察阶段拿不到候选景点 → 兜底
    graph.add_conditional_edges(
        "scout_poi",
        lambda s: "fallback" if not s.get("candidates") else "retrieve",
        {"fallback": "fallback", "retrieve": "retrieve"},
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
