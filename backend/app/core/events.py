"""流式事件协议定义。

后端通过 SSE 推送这些事件，前端 Pinia 按 type 增量更新状态并触发 ECharts 重绘。
新增事件类型时需同步 frontend/src/types/index.ts 的 SSEEventMap。
"""

from typing import Any, Literal

from ..models.schemas import (
    Budget,
    DayPlan,
    StreamChart,
    StreamDay,
    StreamDone,
    StreamError,
    StreamMeta,
    StreamPatch,
    WeatherInfo,
)

EventType = Literal["meta", "trace", "day", "patch", "chart", "done", "error"]


def sse(event: str, data: Any) -> str:
    """序列化为 SSE 帧。

    帧格式要求每行以 \\n 分隔、以空行结尾；data 中若含换行必须拆分，
    否则会被解析成多个 data 行导致 JSON 截断。
    """
    import json

    if not isinstance(data, (dict, list)):
        data = {"value": data}
    payload = json.dumps(data, ensure_ascii=False)
    lines = payload.split("\n")
    body = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event}\n{body}\n\n"


def emit_meta(
    city: str, start_date: str, days: int, request_id: str, preferences: list[str]
) -> str:
    return sse("meta", StreamMeta(
        city=city,
        start_date=start_date,
        days=days,
        request_id=request_id,
        preferences=preferences,
    ).model_dump())


def emit_trace(
    stage: str, name: str, status: str, duration_ms: int | None = None, detail: str = ""
) -> str:
    return sse("trace", {
        "stage": stage,
        "name": name,
        "status": status,
        "duration_ms": duration_ms,
        "detail": detail,
    })


def emit_day(day_payload: dict) -> str:
    return sse("day", StreamDay(day=day_payload).model_dump())


def emit_patch(
    day_index: int, attraction_index: int, lng: float, lat: float, corrected: bool = True
) -> str:
    return sse("patch", StreamPatch(
        day_index=day_index,
        attraction_index=attraction_index,
        location={"longitude": lng, "latitude": lat},
        corrected=corrected,
    ).model_dump())


def emit_chart(
    budget: Budget, weather: list[WeatherInfo], days: list[DayPlan]
) -> str:
    return sse("chart", StreamChart(
        budget=budget,
        weather=weather,
        days=days,
    ).model_dump())


def emit_done(
    total_duration_ms: int,
    days_generated: int,
    attractions_count: int,
    corrected_count: int,
    degraded: bool,
    llm_calls: int = 0,
) -> str:
    return sse("done", StreamDone(
        total_duration_ms=total_duration_ms,
        days_generated=days_generated,
        attractions_count=attractions_count,
        corrected_count=corrected_count,
        degraded=degraded,
        llm_calls=llm_calls,
    ).model_dump())


def emit_error(message: str, stage: str = "", degraded: bool = True) -> str:
    return sse("error", StreamError(
        message=message, stage=stage, degraded=degraded
    ).model_dump())