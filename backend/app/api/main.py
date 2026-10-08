"""FastAPI 应用入口。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import history, poi, trip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 降噪：httpx（含 LangChain 用的 httpx2）会把每个 HTTP 请求的完整 URL
# 打成一行 INFO，一次行程几十条，把真正有用的业务日志全淹没了。
# 降到 WARNING 后只在外部接口真正报错时才出现。
for _noisy in ("httpx", "httpx2", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：退出时关闭 MCP 持久会话，终止 MCP server 子进程。"""
    yield
    try:
        from ..tools.mcp_client import get_mcp_client

        await get_mcp_client().aclose()
        logger.info("MCP 会话已清理")
    except Exception:  # 清理失败不影响退出
        pass


app = FastAPI(
    title="途策智游 · AI 行程规划 API",
    description=(
        "基于 LangGraph 多智能体编排 + MCP 工具调用的 AI 旅行规划服务。\n\n"
        "核心能力：**SSE 流式生成** —— 行程按天渐进下发，"
        "Scout / Planner / Reviewer 多 Agent 协作，"
        "配合图表实现边生成边可视化的体验。"
    ),
    version="2.0.0",
    lifespan=lifespan,
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