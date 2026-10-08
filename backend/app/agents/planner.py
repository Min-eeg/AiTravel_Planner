"""行程规划主服务：把 LangGraph 的状态流转翻译成 SSE 事件流。

职责边界：
- graph.py 只管"流程怎么走"（状态、节点、条件边）
- 本文件只管"怎么把过程播给前端"（SSE 事件构造 + 状态收敛）

这样拆分的好处：编排逻辑可独立单测，流式输出可独立替换（比如换成 WebSocket）。
"""

import json
import logging
import time
import uuid
from datetime import date, timedelta
from typing import Any, AsyncIterator, Dict, List, Tuple

from ..agents.graph import (
    CONSTRAINTS_PROMPT,
    MAX_DAY_RETRIES,
    TripState,
    build_graph,
    settle_budget,
)
from ..core import events
from ..core.config import get_settings
from ..models.drafts import DayDraft, ReviewDraft, SearchPlanDraft
from ..models.schemas import (
    Attraction,
    DayPlan,
    Location,
    Meal,
    TripRequest,
    WeatherInfo,
)
from ..services.amap_service import _normalize_poi, get_amap_service
from ..services.llm_service import LLMError, get_llm_service
from ..services.rag_service import retrieve_knowledge
from ..services.weather_service import get_weather_service
from ..tools.mcp_client import get_mcp_client

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


SCOUT_PROMPT = """你是景点侦察专家。你的任务不是直接找景点，而是为工具检索制定计划：
根据目的地与用户偏好，产出一组高德 POI 搜索关键词，让检索覆盖用户的全部兴趣维度。

要求：
1. 3-5 组关键词，按与用户兴趣的匹配度从高到低排序
2. 关键词要适合地图 POI 搜索（如「博物馆」「历史古迹」「特色美食街」「城市公园」），
   不要用完整句子或过长修饰词
3. 必须覆盖用户给出的每一个兴趣标签，可以适当补充通用维度
4. 不要与「需要避开」的内容相关

输出格式（必须严格遵循，字段名不可改动、不可增减）：
{
  "搜索关键词": ["博物馆", "历史古迹", "特色美食街"]
}
"""

REVIEW_PROMPT = """你是行程质检评审专家。请审查下面这一天的行程安排是否合格。

审查维度：
1. 景点是否与用户兴趣匹配，节奏是否符合要求
2. 同一天的景点是否地理邻近、游览总时长是否合理
3. 三餐安排是否有本地特色、消费预估是否合理
4. 是否违反了用户「需要避开」的要求

判断要宽松：小瑕疵不算不合格，只有明显影响体验的问题才打回。

输出格式（必须严格遵循，字段名不可改动、不可增减）：
{
  "是否合格": true,
  "问题列表": [],
  "修改建议": ""
}
"""


class TripPlanner:
    """流式行程规划器：约束 Agent + 侦察 Agent + 规划 Agent + 评审 Agent 的协作入口。"""

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
            "search_plan": [],
            "candidates": [],
            "weather": [],
            "knowledge": "",
            "review_verdict": "",
            "review_feedback": "",
            "day_retries": {},
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

        # trace 的 duration_ms 是各节点真实耗时：update 到达时刻与上一个
        # update 的间隔即该节点（或同批次并行节点）的实际执行时长。
        # 前端 TracePanel 会展示这个数字，等价于一份内置的性能剖面。
        stage_started = time.time()

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
                    # 前端按 day_index 做 upsert（已存在的同天会被原地替换），
                    # 因此评审打回重做后的修正版也能推给前端，无重复卡片
                    for day in patch.get("days", []) or []:
                        collected_days = _upsert_day(collected_days, day)
                        yield events.emit_day(day.model_dump())

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
                        duration_ms=int((time.time() - stage_started) * 1000),
                        detail=patch.get("detail", ""),
                    )
                stage_started = time.time()

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

    async def _node_scout_poi(self, state: TripState) -> Dict[str, Any]:
        """景点侦察 Agent：LLM 生成检索计划 → MCP 工具执行 → 多路合并去重。

        与「一次关键词搜到底」的差别在于 Agent 化的两步：
        1. 决策——LLM 根据兴趣标签产出一组搜索关键词（SearchPlanDraft），
           兴趣越多元检索覆盖越全，这是「Agent 决定调什么工具」
        2. 执行——每组关键词走一次工具调用：MCP 优先、REST 直连降级，
           结果按名称去重合并，这是「工具真正执行」

        无 LLM（mock 模式）时退化为确定性检索计划，保证评测可跑。
        """
        import asyncio

        request: TripRequest = state["request"]
        constraints = state.get("constraints", {})
        interests = constraints.get("interests") or request.preferences or []
        mcp = get_mcp_client()

        # —— 第一步：检索计划（Agent 决策）——
        queries: List[str] = []
        if self.llm.available and not self.settings.use_mock:
            try:
                plan: SearchPlanDraft = await self.llm.structured(
                    SCOUT_PROMPT,
                    f"目的地：{request.city}\n"
                    f"兴趣标签：{'、'.join(interests) or '无'}\n"
                    f"用户补充描述：{request.free_text or '无'}\n"
                    f"需要避开：{'、'.join(constraints.get('must_avoid') or []) or '无'}",
                    SearchPlanDraft,
                    repair_attempts=0,
                )
                queries = [q.strip() for q in plan.queries if q and q.strip()]
            except LLMError as exc:
                logger.warning("检索计划生成失败，用确定性计划：%s", exc)
        if not queries:
            # 确定性计划：兴趣标签逐个检索 + 一个通用维度兜底
            queries = list(dict.fromkeys([*interests[:3], "景点"]))

        # —— 第二步：工具执行（MCP 优先，REST 降级）——
        channels: set = set()

        async def search_one(keyword: str) -> List[Dict[str, Any]]:
            if mcp.available:
                text = await mcp.try_call_tool(
                    self.settings.mcp_poi_tool,
                    # 注意：amap-mcp-server 的参数校验要求 citylimit 是字符串，
                    # 传 bool 会直接被 server 端 pydantic 拒绝（实测踩坑）
                    {
                        "keywords": keyword,
                        "city": request.city,
                        "citylimit": "true",
                    },
                )
                if text:
                    pois = _parse_mcp_pois(text)
                    if pois:
                        channels.add("mcp")
                        return pois
            pois = await self.amap.search_poi(keyword, request.city, offset=6)
            channels.add("rest" if self.amap.available else "mock")
            return pois

        results = await asyncio.gather(
            *(search_one(q) for q in queries), return_exceptions=True
        )

        # —— 合并去重：按名称，先到先得（关键词按优先级排序）——
        merged: Dict[str, Dict[str, Any]] = {}
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("单路 POI 检索失败：%s", result)
                continue
            for poi in result:
                name = (poi.get("name") or "").strip()
                if name and name not in merged:
                    merged[name] = poi
        candidates = list(merged.values())[:15]

        # —— 坐标补全：MCP 发现 + REST 补全的双链路分工 ——
        # 实测 amap-mcp-server 的搜索工具不带坐标（精简返回），
        # 缺坐标的候选按「景点名 + 城市」并发走 REST 地理编码补齐。
        # 实测踩坑一：15 路并发直接触发高德 CUQPS 限流（个人 Key 仅 3 QPS）。
        # 解法：单并发 + 每次请求节流 0.35s，把节奏压在限流阈值以内，
        # 从"发出去被拒再重试"变成"根本不触发"，实测限流次数降为 0。
        # 实测踩坑二：MCP 候选没有照片，而 REST 回查响应里自带——
        # 因此坐标回查升级为完整信息回填，同一次请求顺带补齐照片/地址/票价，
        # 不额外消耗配额。
        missing = [c for c in candidates if not c.get("longitude")]
        enriched = 0
        geo_semaphore = asyncio.Semaphore(1)

        async def _fill_coords(candidate: Dict[str, Any]) -> None:
            nonlocal enriched
            async with geo_semaphore:
                for attempt in range(2):
                    try:
                        info = await self.amap.lookup_poi(
                            candidate["name"], request.city
                        )
                    except Exception:
                        return
                    if info:
                        candidate["longitude"] = info["longitude"]
                        candidate["latitude"] = info["latitude"]
                        for field in ("address", "image_url", "ticket_price"):
                            if info.get(field) and not candidate.get(field):
                                candidate[field] = info[field]
                        enriched += 1
                        await asyncio.sleep(0.35)  # 节流：相邻请求间隔 ≥0.35s
                        return
                    if attempt == 0:
                        await asyncio.sleep(0.7)  # 退避后再试，多数限流可恢复

        if missing:
            await asyncio.gather(*(_fill_coords(c) for c in missing))

        if not candidates:
            return {
                "search_plan": queries,
                "candidates": [],
                "degraded": True,
                "error_message": "未获取到候选景点",
                "detail": f"{len(queries)} 组关键词均无结果，转入兜底",
            }

        return {
            "search_plan": queries,
            "candidates": candidates,
            "detail": (
                f"{len(queries)} 组关键词 · 候选 {len(candidates)} 个"
                f" · 链路 {'/'.join(sorted(channels)) or 'mock'}"
                + (f" · 坐标补全 {enriched}/{len(missing)}" if missing else "")
            ),
        }

    async def _node_retrieve(self, state: TripState) -> Dict[str, Any]:
        """并行检索天气 ∥ RAG 知识库。POI 已由侦察 Agent 完成。"""
        import asyncio

        request: TripRequest = state["request"]
        constraints = state.get("constraints", {})
        candidates = state.get("candidates", [])

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

        # 天气要用景点坐标来查（比城市名精准），候选池已由 scout_poi 就绪；
        # 天气 ∥ 知识库并行，总耗时约等于最慢的一个
        coords = _center_of(candidates)
        weather_result, knowledge_result = await asyncio.gather(
            self.weather.get_forecast(
                latitude=coords[0],
                longitude=coords[1],
                city=request.city,
                days=request.days,
            ),
            asyncio.to_thread(fetch_knowledge),
            return_exceptions=True,
        )
        weather = _unwrap(weather_result, [], "天气查询")
        knowledge = _unwrap(knowledge_result, "", "知识库检索")

        # 没有天气数据时用城市名兜底查一次
        if not weather:
            try:
                weather = await self.weather.get_forecast(
                    city=request.city, days=request.days
                )
            except Exception:
                weather = []

        return {
            "weather": weather,
            "knowledge": knowledge,
            "detail": f"{len(weather)} 天天气 ∥ 知识库 {len(knowledge)} 字",
        }

    async def _node_plan_day(self, state: TripState) -> Dict[str, Any]:
        """生成单日行程（行程规划 Agent）。被条件边循环调用。

        review_feedback 非空说明上一次产出被评审 Agent 打回，
        修改意见会注入提示词，重新生成同一天；生成后即清空反馈。
        """
        request: TripRequest = state["request"]
        pointer: int = state.get("day_pointer", 0)
        day_date = (
            date.fromisoformat(request.start_date) + timedelta(days=pointer)
        ).isoformat()

        candidates: List[Dict] = state.get("candidates", [])
        used: List[str] = state.get("used_names", [])
        feedback: str = state.get("review_feedback", "")

        day_plan = await self._plan_one_day(
            request=request,
            state=state,
            day_index=pointer,
            day_date=day_date,
            candidates=candidates,
            used_names=used,
            feedback=feedback,
        )

        new_names = [a.name for a in day_plan.attractions]

        retry_note = "（评审打回后重做）" if feedback else ""
        # days / used_names 都带 reducer，这里只返回增量即可
        return {
            "days": [day_plan],
            "day_pointer": pointer + 1,
            "used_names": new_names,
            "review_feedback": "",  # 反馈已消费，清空避免污染下一天
            "detail": f"{day_date} · {len(day_plan.attractions)} 景点{retry_note}",
        }

    async def _node_review_day(self, state: TripState) -> Dict[str, Any]:
        """行程评审 Agent：对刚生成的单日行程做质检。

        两级检查：
        1. 硬规则（确定性，必跑）：编造景点名检测、避开项违反、
           游览超时、三餐缺失——LLM 输出的经典问题都能拦住
        2. LLM 评审（可用时）：从体验维度宽松判断

        不合格且还有重试额度 → 打回 plan_day 重做（day_pointer 指回当天，
        修改意见写入 review_feedback）；额度用完则放行，
        避免评审-重做死循环拖垮整体耗时。
        """
        request: TripRequest = state["request"]
        pointer: int = state.get("day_pointer", 0)
        day_idx = max(pointer - 1, 0)
        days: List[DayPlan] = state.get("days", [])
        day = next((d for d in days if d.day_index == day_idx), None)

        if day is None:
            return {
                "review_verdict": "accept",
                "review_feedback": "",
                "detail": "无待评审行程，跳过",
            }

        retries = dict(state.get("day_retries") or {})
        used_retries = int(retries.get(str(day_idx), 0))

        # —— 第一级：硬规则检查（不依赖 LLM，mock 模式也生效）——
        problems = self._rule_check(day, state)
        suggestion = ""

        # —— 第二级：LLM 体验评审（规则全过时才调，省时省钱）——
        if not problems and self.llm.available and not self.settings.use_mock:
            try:
                verdict: ReviewDraft = await self.llm.structured(
                    REVIEW_PROMPT,
                    self._build_review_input(request, state, day),
                    ReviewDraft,
                    repair_attempts=0,
                )
                if not verdict.passed:
                    problems = verdict.problems or ["评审未通过"]
                    suggestion = verdict.suggestion
            except LLMError as exc:
                logger.warning("LLM 评审失败，仅按硬规则放行：%s", exc)

        if not problems:
            return {
                "review_verdict": "accept",
                "review_feedback": "",
                "detail": f"第 {day_idx + 1} 天评审通过",
            }

        if used_retries < MAX_DAY_RETRIES:
            retries[str(day_idx)] = used_retries + 1
            feedback = "；".join(problems[:3])
            if suggestion:
                feedback = f"{feedback}。建议：{suggestion}"
            logger.info(
                "第 %d 天评审打回（第 %d 次）：%s", day_idx + 1, used_retries + 1, feedback
            )
            return {
                "review_verdict": "reject",
                "review_feedback": feedback,
                "day_retries": retries,
                "day_pointer": day_idx,  # 指回当天，重做后重新评审
                "detail": (
                    f"第 {day_idx + 1} 天打回重做（第 {used_retries + 1} 次）："
                    f"{problems[0]}"
                ),
            }

        # 重试额度用完：放行并如实标注（correct_geo 还会做坐标兜底校正）
        return {
            "review_verdict": "accept",
            "review_feedback": "",
            "detail": f"第 {day_idx + 1} 天重试后仍有问题，放行：{problems[0]}",
        }

    def _rule_check(self, day: DayPlan, state: TripState) -> List[str]:
        """单日行程的确定性硬规则检查。返回问题列表，空列表表示通过。"""
        problems: List[str] = []
        candidates = state.get("candidates", [])
        pool_names = {c["name"] for c in candidates}

        if not day.attractions:
            problems.append("当天没有安排任何景点")
            return problems

        # 编造景点检测：名称必须来自候选池（侦察 Agent 的检索结果）
        hallucinated = [a.name for a in day.attractions if a.name not in pool_names]
        if hallucinated:
            problems.append(f"景点「{'、'.join(hallucinated[:2])}」不在候选池中，涉嫌编造")

        # 游览总时长：单日上限 8 小时
        total_minutes = sum(a.visit_duration for a in day.attractions)
        if total_minutes > 480:
            problems.append(f"总游览时长 {total_minutes} 分钟，超过 8 小时上限")

        # 三餐完整性
        meal_types = {m.type for m in day.meals}
        missing = {"breakfast", "lunch", "dinner"} - meal_types
        if missing:
            problems.append(f"缺少餐次：{'、'.join(sorted(missing))}")

        # 用户「需要避开」的内容
        avoid = state.get("constraints", {}).get("must_avoid") or []
        if avoid:
            blob = day.title + "".join(
                a.name + a.category + a.description for a in day.attractions
            )
            hit = [w for w in avoid if w and w in blob]
            if hit:
                problems.append(f"违反了需要避开的要求：{'、'.join(hit)}")

        # 跨天餐饮重复：多天吃完全一样的 = 规划 Agent 偷懒复制。
        # 宽容单餐雷同（连锁早餐情有可原），两餐以上雷同才打回
        prev_meals = {
            m.name.strip()
            for d in (state.get("days") or [])
            if d.day_index < day.day_index
            for m in d.meals
            if m.name
        }
        if prev_meals:
            repeated = [
                m.name for m in day.meals if m.name and m.name.strip() in prev_meals
            ]
            if len(repeated) >= 2:
                problems.append(
                    f"餐饮与前几日重复（{'、'.join(repeated[:2])}），"
                    "应更换为其他当地特色"
                )

        return problems

    def _build_review_input(
        self, request: TripRequest, state: TripState, day: DayPlan
    ) -> str:
        """拼装评审 Agent 的输入：行程 + 约束 + 知识库要点。"""
        constraints = state.get("constraints", {})
        lines = [
            f"城市：{request.city}",
            f"日期：{day.date}（第 {day.day_index + 1} 天）",
            f"节奏：{constraints.get('pace', request.pace)}，"
            f"预算档位：{constraints.get('budget_level', request.budget_level)}",
        ]
        interests = constraints.get("interests") or request.preferences
        if interests:
            lines.append(f"兴趣偏好：{'、'.join(interests)}")
        avoid = constraints.get("must_avoid") or []
        if avoid:
            lines.append(f"需要避开：{'、'.join(avoid)}")

        lines.append("\n当天行程：")
        lines.append(f"主题：{day.title}")
        for a in day.attractions:
            lines.append(f"- {a.name}（{a.category}，游览 {a.visit_duration} 分钟）：{a.description}")
        for m in day.meals:
            lines.append(f"- {m.type}：{m.name}（人均 ¥{m.estimated_cost:.0f}）")
        return "\n".join(lines)

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
        feedback: str = "",
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
            feedback=feedback,
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
    "parse_input": "约束 Agent · 解析旅行需求",
    "scout_poi": "侦察 Agent · 检索计划 + MCP 工具执行",
    "retrieve": "并行检索（天气 ∥ 知识库）",
    "plan_day": "规划 Agent · 逐日生成行程",
    "review_day": "评审 Agent · 单日质检",
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
    feedback: str = "",
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

    # 餐饮多样性：LLM 不给历史就会每天复制同一份菜单（实测踩坑：
    # 北京 2 天行程两天都是护国寺小吃 + 四季民福烤鸭 + 炸酱面）。
    # 把前几天的餐食明确列进提示词并禁止重复，从源头约束。
    used_meals = sorted({
        m.name.strip()
        for d in (state.get("days") or [])
        if d.day_index < day_index
        for m in d.meals
        if m.name
    })
    if used_meals:
        lines.append(
            "\n## 餐饮多样性（重要）\n"
            f"前几日已安排过：{'、'.join(used_meals)}\n"
            "今天的三餐严禁与上面重复，改用当地其他特色"
            "（不同店铺、不同菜系、不同小吃均可）。"
        )

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

    # 评审 Agent 打回时的修改意见：多智能体协作的闭环就在这一段
    if feedback:
        lines.append(
            f"\n## 评审修改意见（上一版被评审 Agent 打回，本轮必须解决）\n{feedback}"
        )

    lines.append(
        "\n## 要求\n"
        "1. 从候选中挑 2-3 个地理邻近的景点，名称必须与候选完全一致\n"
        "2. 餐饮安排必须是 breakfast / lunch / dinner 三项，餐次固定用英文标识，"
        "且不得与前几日的餐食重复\n"
        "3. 每天总游览时长不超过 8 小时\n"
        "4. 严格按系统提示里的 JSON 格式输出，只输出那些字段，"
        "不要输出城市、日期、总结、建议等任何额外字段"
    )
    return "\n".join(lines)


def _parse_mcp_pois(text: str) -> List[Dict[str, Any]]:
    """解析 MCP 工具返回的 POI 文本。

    amap-mcp-server 返回的是高德 API 原始响应的 JSON 文本；
    不同 server 版本的包装形态不定，这里做防御性解析：
    能识别「{pois: [...]}」「[...原始 POI]」「[...已归一化 POI]」三种形态，
    解析失败返回空列表，让调用方降级 REST。
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []

    if isinstance(data, dict):
        data = data.get("pois") or data.get("data") or []

    if not isinstance(data, list):
        return []

    result: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        if "location" in item and isinstance(item.get("location"), str):
            # 高德原始结构，复用 REST 层的归一化
            result.append(_normalize_poi(item))
        elif "longitude" in item and "latitude" in item:
            # 已归一化结构，直接映射
            result.append({**item, "source": item.get("source", "mcp")})
        else:
            # 精简结构：实测 amap-mcp-server 的搜索工具只返回 id/name/address/typecode，
            # 不带坐标（show_fields 参数也会被忽略）。保留名称先入库，
            # 坐标由 scout 节点统一走 REST 地理编码补全
            result.append(
                {
                    "name": item["name"],
                    "address": item.get("address", ""),
                    "longitude": 0.0,
                    "latitude": 0.0,
                    "ticket_price": 0.0,
                    "description": "",
                    "image_url": "",
                    "source": "mcp",
                }
            )
    return result


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

    # 模板餐食按天轮换：即使是兜底行程，也不该每天吃一样的
    meal_pool = {
        "breakfast": ["酒店早餐", "老字号早点铺", "街巷早点摊", "豆浆油条店"],
        "lunch": ["本帮菜馆", "特色小吃店", "手工面馆", "简餐快餐"],
        "dinner": ["当地特色餐厅", "夜市小吃街", "家常菜馆", "火锅店"],
    }

    return DayPlan(
        date=day_date,
        day_index=day_index,
        title=f"第 {day_index + 1} 天",
        description="模板兜底行程（未使用 AI 生成）",
        transportation="公共交通",
        attractions=attractions,
        meals=[
            Meal(
                type=m_type,
                name=meal_pool[m_type][day_index % 4],
                description="",
                estimated_cost=cost,
            )
            for m_type, cost in (
                ("breakfast", 30.0),
                ("lunch", 60.0),
                ("dinner", 80.0),
            )
        ],
    )


_planner: TripPlanner | None = None


def get_trip_planner() -> TripPlanner:
    """全局单例：LangGraph 图只编译一次，避免重复建图开销。"""
    global _planner
    if _planner is None:
        _planner = TripPlanner()
    return _planner