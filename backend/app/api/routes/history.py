"""行程历史路由：保存 / 列表 / 详情 / 删除。"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...db.history import get_history_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trips", tags=["history"])


class SaveRequest(BaseModel):
    """保存行程请求。

    plan 直接用 dict 接收而非 TripPlan 模型 —— 前端可能带上
    额外字段（如 SSE 内部状态），用 dict 更宽容，避免因字段不匹配而 422。
    """

    plan: dict = Field(..., description="完整行程对象")
    preferences: list[str] = Field(default_factory=list)


@router.post("")
async def save_trip(payload: SaveRequest) -> dict:
    """保存行程到历史。内容重复时返回已有记录，不重复创建。"""
    if not payload.plan:
        raise HTTPException(status_code=400, detail="行程内容为空")

    service = get_history_service()
    result = service.save(payload.plan, payload.preferences)

    return {
        "success": True,
        "trip_id": result["trip_id"],
        "duplicate": result["duplicate"],
        "message": "该行程已保存过" if result["duplicate"] else "保存成功",
    }


@router.get("")
async def list_trips(limit: int = 50) -> dict:
    """行程历史列表（摘要，不含完整行程以减小体积）。"""
    service = get_history_service()
    items = service.list(limit=limit)
    return {"success": True, "total": len(items), "items": items}


@router.get("/{trip_id}")
async def get_trip(trip_id: int) -> dict:
    """行程详情。"""
    service = get_history_service()
    result = service.detail(trip_id)
    if result is None:
        raise HTTPException(status_code=404, detail="行程不存在")
    return {"success": True, **result}


@router.delete("/{trip_id}")
async def delete_trip(trip_id: int) -> dict:
    """删除行程。"""
    service = get_history_service()
    ok = service.delete(trip_id)
    if not ok:
        raise HTTPException(status_code=404, detail="行程不存在")
    return {"success": True, "message": "已删除"}