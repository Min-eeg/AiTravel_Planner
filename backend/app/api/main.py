"""FastAPI 应用入口。"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import history, poi, trip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="途策智游 · AI 行程规划 API",
    description=(
        "基于 LangGraph 多阶段编排的 AI 旅行规划服务。\n\n"
        "核心能力：**SSE 流式生成** —— 行程按天渐进下发，"
        "配合 ECharts 实现边生成边可视化的体验。"
    ),
    version="1.0.0",
)

# 开发期放开跨域；生产环境应改为具体域名白名单
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(trip.router)
app.include_router(poi.router)
app.include_router(history.router)



@app.get("/")
async def root() -> dict:
    return {
        "service": "途策智游 AI 行程规划",
        "docs": "/docs",
        "stream_endpoint": "POST /api/trip/stream",
    }