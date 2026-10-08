"""行程历史持久化：SQLAlchemy + SQLite。

设计要点：
- 存「用户主动保存的行程」，与缓存严格分工（缓存存短期可复用的外部查询结果）
- 整份行程以 JSON 字段存储，不拆成关系表 —— 行程结构本身是嵌套且会演进的，
  拆表带来的 schema 迁移成本远大于收益
- 用 plan_hash 做内容去重，重复保存同一份行程不产生新记录
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from ..core.config import get_settings

DB_PATH = Path(__file__).resolve().parents[2] / "trip_planner.db"


class Base(DeclarativeBase):
    pass


class TripRecord(Base):
    """已保存的行程。"""

    __tablename__ = "trips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(String(64), nullable=False, index=True)
    start_date = Column(String(16), nullable=False)
    days_count = Column(Integer, nullable=False, default=1)
    # 行程完整 JSON；用 Text 而非 JSON 类型以兼容 SQLite
    plan_json = Column(Text, nullable=False)
    preferences_json = Column(Text, nullable=False, default="[]")
    # 行程内容哈希，用于去重
    plan_hash = Column(String(64), nullable=False, index=True)
    total_budget = Column(Integer, nullable=False, default=0)
    degraded = Column(Integer, nullable=False, default=0)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TripHistoryService:
    """行程历史 CRUD。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.db_path = DB_PATH
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            # SSE 长连接下连接不能长期占用，否则历史接口会被阻塞
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    # ========== 内部工具 ==========

    @staticmethod
    def _hash_plan(plan: Dict[str, Any]) -> str:
        """对行程内容做稳定哈希（排序后序列化，保证同内容同哈希）。"""
        raw = json.dumps(plan, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    # ========== 写操作 ==========

    def save(
        self, plan: Dict[str, Any], preferences: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """保存行程。内容重复时返回已有记录，不新建。

        返回 {"trip_id": int, "duplicate": bool}
        """
        plan_hash = self._hash_plan(plan)
        prefs = preferences or []

        with self._session_factory() as session:
            existing = session.execute(
                select(TripRecord).where(TripRecord.plan_hash == plan_hash)
            ).scalar_one_or_none()

            if existing:
                return {"trip_id": existing.id, "duplicate": True}

            days = plan.get("days") or []
            record = TripRecord(
                city=plan.get("city", "未知"),
                start_date=plan.get("start_date", ""),
                days_count=len(days),
                plan_json=json.dumps(plan, ensure_ascii=False),
                preferences_json=json.dumps(prefs, ensure_ascii=False),
                plan_hash=plan_hash,
                total_budget=int(plan.get("budget", {}).get("total", 0)),
                degraded=1 if plan.get("degraded") else 0,
            )
            session.add(record)
            session.commit()
            return {"trip_id": record.id, "duplicate": False}

    def delete(self, trip_id: int) -> bool:
        """删除行程。返回是否真的删掉了。"""
        with self._session_factory() as session:
            record = session.get(TripRecord, trip_id)
            if record is None:
                return False
            session.delete(record)
            session.commit()
            return True

    # ========== 读操作 ==========

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        """列出历史行程摘要（不含完整 plan，避免列表过重）。"""
        with self._session_factory() as session:
            records = session.execute(
                select(TripRecord).order_by(TripRecord.created_at.desc()).limit(limit)
            ).scalars().all()

            return [
                {
                    "id": r.id,
                    "city": r.city,
                    "start_date": r.start_date,
                    "days_count": r.days_count,
                    "preferences": json.loads(r.preferences_json or "[]"),
                    "total_budget": r.total_budget,
                    "degraded": bool(r.degraded),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ]

    def detail(self, trip_id: int) -> Optional[Dict[str, Any]]:
        """取完整行程。"""
        with self._session_factory() as session:
            record = session.get(TripRecord, trip_id)
            if record is None:
                return None

            return {
                "summary": {
                    "id": record.id,
                    "city": record.city,
                    "start_date": record.start_date,
                    "days_count": record.days_count,
                    "preferences": json.loads(record.preferences_json or "[]"),
                    "total_budget": record.total_budget,
                    "degraded": bool(record.degraded),
                    "created_at": record.created_at.isoformat()
                    if record.created_at
                    else None,
                },
                "plan": json.loads(record.plan_json),
            }


_service: Optional[TripHistoryService] = None


def get_history_service() -> TripHistoryService:
    """全局单例（engine 与 session factory 只建一次）。"""
    global _service
    if _service is None:
        _service = TripHistoryService()
    return _service