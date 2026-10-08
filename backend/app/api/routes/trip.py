"""行程相关路由：流式生成（SSE）与非流式生成。

一个容易踩的坑：SSE 端点必须用 `async def` + 异步生成器，
否则 FastAPI 会把整个生成过程当作同步任务执行，事件无法增量下发。
"""

import json
import logging
from contextlib import suppress
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ...agents.planner import get_trip_planner
from ...models.schemas import Budget, DayPlan, TripPlan, TripRequest, WeatherInfo
from ...services.rag_service import get_knowledge_base

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trip", tags=["trip"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 禁用 Nginx 缓冲，否则事件会被攒着一次性下发，流式就失效了
    "X-Accel-Buffering": "no",
}


@router.post("/stream")
async def stream_trip(request: TripRequest) -> StreamingResponse:
    """流式生成行程。

    事件类型：meta / trace / day / patch / chart / done / error
    """
    planner = get_trip_planner()

    async def event_generator() -> AsyncIterator[bytes]:
        try:
            async for chunk in planner.stream_plan(request):
                yield chunk.encode("utf-8")
        except Exception as exc:
            # 兜底：生成器本身异常也要以 SSE 帧告知前端，而不是直接断开连接
            logger.exception("SSE 流异常")
            payload = {
                "message": str(exc)[:300],
                "stage": "stream",
                "degraded": True,
            }
            frame = f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield frame.encode("utf-8")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/plan", response_model=TripPlan)
async def plan_trip(request: TripRequest) -> TripPlan:
    """非流式生成行程（供脚本 / 评测调用）。

    内部复用同一套编排逻辑，只是把事件流收集起来拼成完整结果，
    保证两条链路行为一致。

    注意：必须用 SSE 的 event 字段来分派，不能靠 payload 里有没有某个 key
    —— 因为 `days_generated` 这类字段会让 `'days' in payload` 误判为True。
    """
    planner = get_trip_planner()
    result = TripPlan(
        city=request.city,
        start_date=request.start_date,
        days=[],
        degraded=False,
    )

    async for chunk in planner.stream_plan(request):
        for event, payload in parse_sse_frames(chunk):
            if event == "day":
                result.days.append(DayPlan(**payload["day"]))
            elif event == "chart":
                result.budget = Budget(**payload["budget"])
                result.weather = [WeatherInfo(**w) for w in payload.get("weather", [])]
            elif event == "error":
                result.degraded = True
            elif event == "done":
                result.degraded = bool(payload.get("degraded", False))

    if not result.days:
        raise HTTPException(status_code=502, detail="行程生成失败")

    return result


def parse_sse_frames(chunk: str) -> list[tuple[str, dict]]:
    """把一帧 SSE 文本解析成 (event, payload) 列表。

    与前端 stream.ts 的 parseFrame 逻辑对齐，但用 event 字段分派更可靠。
    """
    frames: list[tuple[str, dict]] = []
    event = ""
    data_lines: list[str] = []

    for line in chunk.split("\n"):
        if line.startswith("event: "):
            if event and data_lines:
                with suppress(json.JSONDecodeError):
                    frames.append((event, json.loads("\n".join(data_lines))))
            event = line[7:].strip()
            data_lines = []
        elif line.startswith("data: "):
            data_lines.append(line[6:])
        elif not line.strip() and (event or data_lines):
            # 空行表示帧结束
            if event and data_lines:
                with suppress(json.JSONDecodeError):
                    frames.append((event, json.loads("\n".join(data_lines))))
            event = ""
            data_lines = []

    # 收尾
    if event and data_lines:
        with suppress(json.JSONDecodeError):
            frames.append((event, json.loads("\n".join(data_lines))))

    return frames


@router.get("/knowledge/cities")
async def list_cities() -> dict:
    """已覆盖 RAG 知识库的城市列表，供前端做输入提示。"""
    return {"cities": get_knowledge_base().list_cities()}


@router.get("/health")
async def health() -> dict:
    """健康检查，同时暴露依赖可用性便于排查。"""
    from ...core.cache import cache
    from ...core.config import get_settings

    settings = get_settings()
    return {
        "status": "ok",
        "llm_ready": settings.has_llm,
        "amap_ready": settings.has_amap,
        "mock_mode": settings.use_mock,
        "rag_enabled": settings.rag_enabled,
        "cache_hit_rate": getattr(cache, "hit_rate", 0.0),
        "kb_cities": get_knowledge_base().list_cities(),
    }