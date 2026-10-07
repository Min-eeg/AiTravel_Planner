"""行程规划主服务：把 LangGraph 的状态流转翻译成 SSE 事件流。

职责边界：
- graph.py 只管"流程怎么走"（状态、节点、条件边）
- 本文件只管"怎么把过程播给前端"（SSE 事件构造 + 状态收敛）

这样拆分的好处：编排逻辑可独立单测，流式输出可独立替换（比如换成 WebSocket）。
"""

import logging
import time
import uuid
from datetime import date, timedelta
from typing import Any, AsyncIterator, Dict, List, Tuple

from ..agents.graph import CONSTRAINTS_PROMPT, TripState, build_graph, settle_budget
from ..core import events
from ..core.config import get_settings
from ..models.drafts import DayDraft
from ..models.schemas import (
    Attraction,
    DayPlan,
    Location,
    Meal,
    TripRequest,
    WeatherInfo,
)
from ..services.amap_service import get_amap_service
from ..services.llm_service import LLMError, get_llm_service
from ..services.rag_service import retrieve_knowledge
from ..services.weather_service import get_weather_service

logger = logging.getLogger(__name__)


DAY_PROMPT = """你是资深旅行规划专家，为用户规划某一天的行程。

硬性规则：
1. 景点必须从「可用候选」中挑选，严禁编造景点名称，必须与候选名称完全一致
2. 每天安排 2-3 个景点，尽量地理邻近，控制总游览时长在 8 小时内
3. 餐饮必须是三项 breakfast / lunch / dinner
4. 如果知识库提到某景点闭馆日或需预约，避开或说明
5. 只输出规定的 JSON 结构，不要添加城市、日期、总结等任何额外字段

输出格式（必须严格遵循，字段名不可改动、不可增减）：
{
  "当天主题": "如「西湖环线与老城漫步」",
  "当天概述": "50 字以内的当天概述",
  "景点安排": [
    {
      "景点名称": "必须与候选列表完全一致",
      "游览分钟": 90,
      "看点介绍": "40 字以内的看点",
      "类别": "文化古迹"
    }
  ],
  "餐饮安排": [
    {"餐次": "breakfast", "餐名": "推荐餐食", "特色说明": "本地特色", "预估人均消费": 25},
    {"餐次": "lunch", "餐名": "推荐餐食", "特色说明": "", "预估人均消费": 60},
    {"餐次": "dinner", "餐名": "推荐餐食", "特色说明": "", "预估人均消费": 80}
  ]
}
"""


class TripPlanner:
    """流式行程规划器。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.llm = get_llm_service()
        self.amap = get_amap_service()
        self.weather = get_weather_service()
        self.graph = build_graph(self)

    # ==================== 对外接口 ====================

    async def stream_plan(
        self, request: TripRequest, request_id: str | None = None
    ) -> AsyncIterator[str]:
        """执行规划流程，逐帧 yield SSE 数据。"""
        rid = request_id or uuid.uuid4().hex[:12]
        started = time.time()

        # meta 先发：前端立即拿到骨架开始渲染，不等后续内容
        yield events.emit_meta(
            city=request.city,
            start_date=request.start_date,
            days=request.days,
            request_id=rid,
            preferences=request.preferences,
        )

        initial: TripState = {
            "request": request,
            "constraints": {
                "pace": request.pace,
                "budget_level": request.budget_level,
                "interests": list(request.preferences),
                "must_avoid": [],
            },
            "candidates": [],
            "weather": [],
            "knowledge": "",
            "days": [],
            "day_pointer": 0,
            "used_names": [],
            "corrected_count": 0,
            "degraded": False,
            "error_message": "",
            "started_at": started,
            "detail": "",
        }

        # 本地累积状态。
        # 注意：astream(stream_mode="updates") 只推送各节点的增量 patch，
        # 不是累积后的完整 state，所以 days/used_names/corrections 必须在这里
        # 自己按 reducer 语义累加，否则最终预算只会算到最后一天。
        collected_days: List[DayPlan] = []
        collected_corrections: List[Dict[str, Any]] = []
        latest_constraints: Dict[str, Any] = dict(initial["constraints"])
        latest_weather: List[Dict[str, Any]] = []
        corrected_count = 0
        is_degraded = False
        emitted_days = 0

        # 各阶段预计耗时（毫秒），在阶段开始前告知前端。
        # 真实 LLM 调用要 30-60s，如果用户等待期间界面毫无变化，
        # 就会以为是卡死了。给出预估时长能让进度条动起来，体验完全不同。
        eta = {
            "parse_input": 1500,
            "retrieve": 1200,
            "plan_day": 20000,
            "correct_geo": 2500,
            "finalize": 300,
        }

        try:
            async for update in self.graph.astream(
                initial, stream_mode="updates"
            ):
                # update 形如 {"plan_day": {"days": [当天], "day_pointer": 1}}
                for node_name, patch in update.items():
                    if not isinstance(patch, dict):
                        continue

                    # 标量字段：后写覆盖
                    if "constraints" in patch:
                        latest_constraints.update(patch["constraints"])
                    if "weather" in patch:
                        latest_weather = patch["weather"]
                    if "degraded" in patch:
                        is_degraded = bool(patch["degraded"])
                    if "corrected_count" in patch:
                        corrected_count = patch["corrected_count"]

                    # —— 逐日渲染：节点产出新的一天就下发 ——
                    for day in patch.get("days", []) or []:
                        collected_days = _upsert_day(collected_days, day)
                        if emitted_days < day.day_index + 1:
                            yield events.emit_day(day.model_dump())
                            emitted_days += 1

                    # —— 坐标校正补丁 ——
                    for correction in patch.get("corrections", []) or []:
                        collected_corrections.append(correction)
                        yield events.emit_patch(
                            day_index=correction["day_index"],
                            attraction_index=correction["attraction_index"],
                            lng=correction["longitude"],
                            lat=correction["latitude"],
                        )

                    yield events.emit_trace(
                        node_name,
                        STAGE_LABEL.get(node_name, node_name),
                        "ok",
                        detail=patch.get("detail", ""),
                    )

            # —— chart：结算完成的数据快照（含写回住宿/交通后的完整逐日数据） ——
            days = collected_days
            budget_level = latest_constraints.get("budget_level", request.budget_level)
            budget = settle_budget(days, budget_level, request.city)
            weather = [WeatherInfo(**w) for w in latest_weather]
            yield events.emit_chart(budget, weather, days)

            yield events.emit_done(
                total_duration_ms=int((time.time() - started) * 1000),
                days_generated=len(days),
                attractions_count=sum(len(d.attractions) for d in days),
                corrected_count=corrected_count,
                degraded=is_degraded,
                llm_calls=self.llm.call_count,
            )

        except Exception as exc:
            # 顶层兜底：任何未预期异常都转成 error 事件 + 模板行程，绝不给白屏
            logger.exception("规划流程异常")
            yield events.emit_error(str(exc)[:300], stage="graph", degraded=True)

            plan_days = self._fallback_days(request)
            for day in plan_days:
                yield events.emit_day(day.model_dump())
            yield events.emit_trace("fallback", "已降级为模板行程", "failed")

            budget_level = request.budget_level
            budget = settle_budget(plan_days, budget_level, request.city)
            try:
                weather = [
                    WeatherInfo(**w)
                    for w in await self.weather.get_forecast(
                        city=request.city, days=request.days
                    )
                ]
            except Exception:
                weather = []
            yield events.emit_chart(budget, weather, plan_days)
            yield events.emit_done(
                total_duration_ms=int((time.time() - started) * 1000),
                days_generated=len(plan_days),
                attractions_count=sum(len(d.attractions) for d in plan_days),
                corrected_count=0,
                degraded=True,
                llm_calls=self.llm.call_count,
            )

    # ==================== LangGraph 节点实现 ====================

    async def _node_parse_input(self, state: TripState) -> Dict[str, Any]:
        request: TripRequest = state["request"]
        base = {
            "pace": request.pace,
            "budget_level": request.budget_level,
            "interests": list(request.preferences),
            "must_avoid": [],
            "notes": "",
        }
        if not request.free_text or not self.llm.available:
            return {"constraints": base, "detail": "使用默认约束"}

        try:
            from ..models.drafts import ConstraintsDraft

            draft: ConstraintsDraft = await self.llm.structured(
                CONSTRAINTS_PROMPT,
                f"目的地：{request.city}\n天数：{request.days}天\n"
                f"偏好标签：{'、'.join(request.preferences) or '无'}\n"
                f"用户自由描述：{request.free_text}",
                ConstraintsDraft,
                repair_attempts=0,
            )

            # LLM 只做「补充」，不推翻用户显式选择：
            # 用户在下拉框里选的节奏/预算是明确意图，
            # 而模型可能因为描述里的只言片语就改判（比如"想住好点但预算紧"就自相矛盾）。
            # 因此只有当用户没选时才采纳模型推断。
            pace = request.pace or draft.pace
            budget_level = request.budget_level or draft.budget_level

            # 兴趣标签合并：用户勾选的 + 模型从自由描述抽出的，去重且保序
            interests: List[str] = list(request.preferences)
            for tag in draft.interests:
                if tag and tag not in interests:
                    interests.append(tag)

            return {
                "constraints": {
                    "pace": pace,
                    "budget_level": budget_level,
                    "interests": interests,
                    "must_avoid": draft.must_avoid,
                    "notes": draft.notes,
                },
                "detail": (
                    f"兴趣标签 {len(interests)} 个"
                    + (f"（模型补充 {len(draft.interests)} 个）" if draft.interests else "")
                ),
            }
        except LLMError as exc:
            logger.warning("约束解析失败，用默认值：%s", exc)
            return {"constraints": base, "detail": "约束解析失败，用默认值"}

    async def _node_retrieve(self, state: TripState) -> Dict[str, Any]:
        request: TripRequest = state["request"]
        constraints = state.get("constraints", {})
        keywords = (constraints.get("interests") or request.preferences or ["景点"])[0]

        import asyncio

        async def fetch_poi():
            return await self.amap.search_poi(keywords, request.city, offset=10)

        def fetch_knowledge():
            if not self.settings.rag_enabled:
                return ""
            query = " ".join(
                [
                    request.city,
                    " ".join(constraints.get("interests") or request.preferences),
                    request.free_text,
                ]
            )
            return retrieve_knowledge(request.city, query, top_k=4)

        # POI 必须先拿到：天气要用景点坐标来查（比城市名精准）。
        # 因此这里的并行度是「POI ∥ 知识库」两路；
        # 天气在 POI 之后查（单次约 300ms，相比 LLM 的秒级耗时可忽略）。
        poi_result, knowledge_result = await asyncio.gather(
            fetch_poi(), asyncio.to_thread(fetch_knowledge), return_exceptions=True
        )
        candidates = _unwrap(poi_result, [], "POI 检索")
        knowledge = _unwrap(knowledge_result, "", "知识库检索")

        coords = _center_of(candidates)
        weather = _unwrap(
            await self.weather.get_forecast(
                latitude=coords[0],
                longitude=coords[1],
                city=request.city,
                days=request.days,
            ),
            [],
            "天气查询",
        )

        # 没有候选景点时用城市名兜底查一次天气
        if not weather:
            try:
                weather = await self.weather.get_forecast(
                    city=request.city, days=request.days
                )
            except Exception:
                weather = []

        if not candidates:
            # 无候选景点直接走兜底，避免 plan_day 空转
            return {
                "candidates": [],
                "weather": weather,
                "knowledge": knowledge,
                "degraded": True,
                "error_message": "未获取到候选景点",
                "detail": "无候选景点，转入兜底",
            }

        return {
            "candidates": candidates,
            "weather": weather,
            "knowledge": knowledge,
            "detail": f"{len(candidates)} 景点 ∥ {len(weather)} 天天气 ∥ 知识库 {len(knowledge)} 字",
        }

    async def _node_plan_day(self, state: TripState) -> Dict[str, Any]:
        """生成单日行程。被条件边循环调用。"""
        request: TripRequest = state["request"]
        pointer: int = state.get("day_pointer", 0)
        day_date = (
            date.fromisoformat(request.start_date) + timedelta(days=pointer)
        ).isoformat()

        candidates: List[Dict] = state.get("candidates", [])
        used: List[str] = state.get("used_names", [])

        day_plan = await self._plan_one_day(
            request=request,
            state=state,
            day_index=pointer,
            day_date=day_date,
            candidates=candidates,
            used_names=used,
        )

        new_names = [a.name for a in day_plan.attractions]

        # days / used_names 都带 reducer，这里只返回增量即可
        return {
            "days": [day_plan],
            "day_pointer": pointer + 1,
            "used_names": new_names,
            "detail": f"{day_date} · {len(day_plan.attractions)} 景点",
        }

    async def _node_correct_geo(self, state: TripState) -> Dict[str, Any]:
        """用真实 POI 坐标覆盖 LLM 编造坐标。

        LLM 的经纬度基本不可信（直接展示会地图错位、路线乱串），
        这里按「景点名 + 城市」回查真实坐标；查不到保留原值，不阻断流程。
        """
        import asyncio

        request: TripRequest = state["request"]
        days: List[DayPlan] = state.get("days", [])

        if not self.amap.available or not days:
            return {"corrected_count": 0, "corrections": [], "detail": "跳过校正"}

        by_name = {c["name"]: c for c in state.get("candidates", [])}

        corrections: List[Dict[str, Any]] = []
        count = 0

        async def correct_one(day_idx: int, attr_idx: int, attr: Attraction) -> None:
            nonlocal count
            # 优先用候选池里已有的真实坐标（检索阶段就已拿到）
            source = by_name.get(attr.name)
            if source and source.get("longitude"):
                if not attr.location or _differs(attr.location, source):
                    attr.location = Location(
                        longitude=source["longitude"], latitude=source["latitude"]
                    )
                    attr.location_corrected = True
                    count += 1
                    corrections.append(
                        {
                            "day_index": day_idx,
                            "attraction_index": attr_idx,
                            "longitude": source["longitude"],
                            "latitude": source["latitude"],
                        }
                    )
                return

            # 候选池没有就回查高德
            try:
                loc = await self.amap.geocode_poi(attr.name, request.city)
                if loc:
                    attr.location = Location(**loc)
                    attr.location_corrected = True
                    count += 1
                    corrections.append(
                        {
                            "day_index": day_idx,
                            "attraction_index": attr_idx,
                            **loc,
                        }
                    )
            except Exception as exc:
                logger.debug("坐标校正失败 %s：%s", attr.name, exc)

        jobs = []
        for d_idx, day in enumerate(days):
            for a_idx, attr in enumerate(day.attractions):
                jobs.append(correct_one(d_idx, a_idx, attr))

        await asyncio.gather(*jobs, return_exceptions=True)

        # days / corrections 都带 reducer：days 直接原地改对象（已在 state 中），
        # corrections 只返回增量
        state["days"] = days
        return {
            "corrected_count": count,
            "corrections": corrections,
            "detail": f"校正 {count} 个景点坐标",
        }

    def _node_finalize(self, state: TripState) -> Dict[str, Any]:
        days: List[DayPlan] = state.get("days", [])
        return {"detail": f"完成 {len(days)} 天行程"}

    def _node_fallback(self, state: TripState) -> Dict[str, Any]:
        request: TripRequest = state["request"]
        days = self._fallback_days(request)
        return {
            "days": days,
            "day_pointer": request.days,
            "degraded": True,
            "error_message": state.get("error_message", "流程转入兜底"),
            "detail": "已生成模板兜底行程",
        }

    # ==================== 单日生成与降级 ====================

    async def _plan_one_day(
        self,
        request: TripRequest,
        state: TripState,
        day_index: int,
        day_date: str,
        candidates: List[Dict],
        used_names: List[str],
    ) -> DayPlan:
        """生成单日。无 LLM 时走确定性模板，保证链路可跑通。"""
        if not self.llm.available:
            return _fallback_day(day_index, day_date, candidates, used_names)

        # 候选池：优先未用过的景点，不足时放开限制
        pool = [c for c in candidates if c["name"] not in used_names] or candidates
        weather = state.get("weather", [])
        day_weather = weather[day_index % len(weather)] if weather else {}

        user_prompt = _build_day_prompt(
            request=request,
            state=state,
            day_index=day_index,
            day_date=day_date,
            pool=pool,
            day_weather=day_weather,
        )

        try:
            draft: DayDraft = await self.llm.structured(
                DAY_PROMPT, user_prompt, DayDraft, repair_attempts=1
            )
            return _build_day_plan(draft, day_index, day_date, pool)
        except LLMError as exc:
            # 单日失败不终止整个行程
            logger.warning("第 %s 天生成失败，降级模板：%s", day_index + 1, exc)
            return _fallback_day(day_index, day_date, candidates, used_names)

    def _fallback_days(self, request: TripRequest) -> List[DayPlan]:
        """全链路模板兜底，不依赖任何外部服务。"""
        candidates = self.amap._mock_poi(request.city, 8)
        used: List[str] = []
        days = []
        for idx in range(request.days):
            day_date = (
                date.fromisoformat(request.start_date) + timedelta(days=idx)
            ).isoformat()
            days.append(_fallback_day(idx, day_date, candidates, used))
        return days


# ============ 辅助函数 ============

STAGE_LABEL = {
    "parse_input": "解析旅行约束",
    "retrieve": "并行检索（POI ∥ 天气 ∥ 知识库）",
    "plan_day": "逐日生成行程",
    "correct_geo": "真实坐标校正",
    "finalize": "预算结算",
    "fallback": "降级兜底",
}


def _unwrap(result: Any, fallback: Any, label: str) -> Any:
    if isinstance(result, BaseException):
        logger.warning("%s 失败：%s", label, result)
        return fallback
    return result


def _center_of(candidates: List[Dict]) -> Tuple[float, float]:
    """候选景点的中心坐标，用作天气查询定位点。

    没有候选时返回杭州默认值（不至于让天气查询直接失败）。
    """
    points = [
        (c["longitude"], c["latitude"])
        for c in candidates
        if c.get("longitude") and c.get("latitude")
    ]
    if not points:
        return (30.2741, 120.1551)
    lng = sum(p[0] for p in points) / len(points)
    lat = sum(p[1] for p in points) / len(points)
    return (lat, lng)  # 注意返回顺序是 (lat, lng)


def _upsert_day(days: List[DayPlan], day: DayPlan) -> List[DayPlan]:
    """按 day_index 插入或替换，并保持按序。

    与 graph.py 的 _merge_days 语义一致：同index 覆盖（可能是重试后的修正），
    不同 index追加。
    """
    merged = {d.day_index: d for d in days}
    merged[day.day_index] = day
    return [merged[i] for i in sorted(merged)]


def _differs(loc: Location, source: Dict[str, Any]) -> bool:
    """判断 LLM 给的坐标是否与真实值不符。"""
    return (
        abs(loc.longitude - source["longitude"]) > 1e-6
        or abs(loc.latitude - source["latitude"]) > 1e-6
    )


def _build_day_prompt(
    request: TripRequest,
    state: TripState,
    day_index: int,
    day_date: str,
    pool: List[Dict],
    day_weather: Dict,
) -> str:
    """构造单日规划提示词。真实坐标已在结构化层处理，这里给名称与描述即可。"""
    constraints = state.get("constraints", {})
    knowledge = state.get("knowledge", "")

    lines = [
        "## 基本信息",
        f"- 城市：{request.city}",
        f"- 日期：{day_date}（第 {day_index + 1} 天 / 共 {request.days} 天）",
        f"- 节奏：{constraints.get('pace', request.pace)}",
        f"- 预算档位：{constraints.get('budget_level', request.budget_level)}",
    ]

    if day_weather:
        lines.append(
            f"- 当天天气：{day_weather.get('day_weather', '未知')}，"
            f"{day_weather.get('day_temp', '?')}°C / {day_weather.get('night_temp', '?')}°C"
        )

    interests = constraints.get("interests") or request.preferences
    if interests:
        lines.append(f"- 兴趣偏好：{'、'.join(interests)}")

    avoid = constraints.get("must_avoid") or []
    if avoid:
        lines.append(f"- 必须避开：{'、'.join(avoid)}")

    notes = constraints.get("notes")
    if notes:
        lines.append(f"- 其他约束：{notes}")

    lines.append("\n## 可用景点候选（只能从中挑选，严禁编造）")
    for c in pool:
        price = c.get("ticket_price", 0)
        lines.append(
            f"- {c['name']}｜门票 {price} 元｜{c.get('description', '')}"
        )

    if knowledge:
        lines.append(
            f"\n## 本地知识库（务必遵守闭馆日、预约、避坑提示）\n{knowledge}"
        )

    lines.append(
        "\n## 要求\n"
        "1. 从候选中挑 2-3 个地理邻近的景点，名称必须与候选完全一致\n"
        "2. 餐饮安排必须是 breakfast / lunch / dinner 三项，餐次固定用英文标识\n"
        "3. 每天总游览时长不超过 8 小时\n"
        "4. 严格按系统提示里的 JSON 格式输出，只输出那些字段，"
        "不要输出城市、日期、总结、建议等任何额外字段"
    )
    return "\n".join(lines)


def _build_day_plan(draft: DayDraft, day_index: int, day_date: str, pool: List[Dict]) -> DayPlan:
    """把结构化草稿映射为 DayPlan，注入真实坐标与门票价。

    这里对 LLM 数值做了一轮合理性校正：模型偶尔会把「2 小时」填进
    分钟字段，或给出离谱的餐饮价格。展示层直接用这些数字会很明显地错，
    所以在落地前夹到合理区间。
    """
    by_name = {c["name"]: c for c in pool}

    attractions = []
    for a in draft.attractions[:4]:
        name = (a.name or "").strip()
        if not name:
            continue
        # 坐标与门票价一律从候选池取真实值，不信任 LLM 输出
        source = by_name.get(name)
        attractions.append(
            Attraction(
                name=name,
                address=(source or {}).get("address", ""),
                location=(
                    Location(
                        longitude=source["longitude"], latitude=source["latitude"]
                    )
                    if source
                    else None
                ),
                visit_duration=_sane_duration(a.visit_duration),
                description=(a.description or "")[:200],
                category=a.category or "景点",
                ticket_price=float((source or {}).get("ticket_price", 0) or 0),
                # 图片来自高德 POI 的真实照片，不用 LLM 生成
                image_url=(source or {}).get("image_url") or None,
            )
        )

    meals = []
    # 用normalized_meals() 统一数组 / 对象两种形态（中文模型常误输出对象）
    for m in draft.normalized_meals():
        mtype = (m.type or "").lower()
        if mtype not in ("breakfast", "lunch", "dinner"):
            continue
        meals.append(
            Meal(
                type=mtype,
                name=(m.name or "")[:60],
                description=(m.description or "")[:120],
                estimated_cost=_sane_cost(m.estimated_cost),
            )
        )

    # 补齐缺失餐次，保证「每天三餐」硬指标
    for required, cost in (("breakfast", 30.0), ("lunch", 60.0), ("dinner", 80.0)):
        if not any(m.type == required for m in meals):
            meals.append(
                Meal(type=required, name=f"{required} 推荐",
                     description="本地特色餐饮", estimated_cost=cost)
            )

    return DayPlan(
        date=day_date,
        day_index=day_index,  # 可推导字段按下标赋值，不依赖 LLM
        title=(draft.title or f"第 {day_index + 1} 天")[:40],
        description=(draft.description or "")[:300],
        transportation="公共交通",
        attractions=attractions,
        meals=meals,
    )


def _sane_duration(value: int | None) -> int:
    """把游览时长夹到合理区间。

    实测模型会把「2 小时」直接填进分钟字段（得到 2），导致前端显示「游览 2 分钟」。
    这里判断：过小则按小时补乘，过大则截断。
    """
    if not value or value <= 0:
        return 120  # 缺省给 2 小时
    if value < 30:
        # 像是把小时数填成了分钟，按小时换算（上限 4 小时）
        return min(value * 60, 240)
    return min(value, 480)


def _sane_cost(value: float | None) -> float:
    """把餐饮单价夹到合理区间，避免出现 ¥0.01 或 ¥9999 这类离谱数字。"""
    if not value or value <= 0:
        return 50.0
    if value < 5:
        return 20.0  # 早餐可能确实便宜，但仍给个下限
    return float(min(value, 500))


def _fallback_day(
    day_index: int, day_date: str, candidates: List[Dict], used_names: List[str]
) -> DayPlan:
    """模板日：无 LLM 或单日失败时使用。"""
    available = [c for c in candidates if c["name"] not in used_names] or candidates
    picked = available[: 2 + (day_index % 2)]

    attractions = []
    for c in picked:
        attractions.append(
            Attraction(
                name=c["name"],
                address=c.get("address", ""),
                location=Location(longitude=c["longitude"], latitude=c["latitude"]),
                visit_duration=120,
                description=c.get("description", ""),
                ticket_price=float(c.get("ticket_price", 0) or 0),
                image_url=c.get("image_url") or None,
            )
        )
    used_names.extend(c["name"] for c in picked)

    return DayPlan(
        date=day_date,
        day_index=day_index,
        title=f"第 {day_index + 1} 天",
        description="模板兜底行程（未使用 AI 生成）",
        transportation="公共交通",
        attractions=attractions,
        meals=[
            Meal(type="breakfast", name="早餐", description="当地特色早餐", estimated_cost=30.0),
            Meal(type="lunch", name="午餐", description="", estimated_cost=60.0),
            Meal(type="dinner", name="晚餐", description="", estimated_cost=80.0),
        ],
    )


_planner: TripPlanner | None = None


def get_trip_planner() -> TripPlanner:
    """全局单例：LangGraph 图只编译一次，避免重复建图开销。"""
    global _planner
    if _planner is None:
        _planner = TripPlanner()
    return _planner