"""MCP (Model Context Protocol) 客户端封装 —— 框架无关。

为什么自己封装而不是直接用 SDK 裸调：
- 持久会话：每次工具调用都 spawn 一个 MCP 子进程再销毁的话，一次行程要
  开关进程十几次；这里子进程只启动一次全程复用，异常自动重建，
  应用退出统一清理（FastAPI lifespan 挂钩）。
- 框架无关：只暴露 `list_tools()` / `call_tool()` 两个异步原语，
  不绑定任何 Agent 框架的类型，LangGraph 或手写编排都能接。
- 失败不抛异常打断主流程：`available` 置 False，调用方自动降级 REST 直连。

一个踩过的深层坑：mcp SDK 的 stdio_client 内部用 anyio cancel scope，
**进入和退出必须在同一个 asyncio Task 里**。最初用 AsyncExitStack
懒创建会话，重建/关闭发生在另一个请求任务里，实测触发
`Attempted to exit cancel scope in a different task`，
之后所有调用 Connection closed。最终方案是「专职守护任务」：
连接的建立与销毁永远在同一个后台任务内完成，业务任务只通过事件与
该任务交互（见 _supervise / _ensure_session）。

设计原则：MCP 是「工具获取协议」，不是必需依赖——
未安装 mcp 包、未配置 server 命令、子进程启动失败，三种情况都只降级、不报错。
"""

import asyncio
import importlib.util
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from ..core.config import get_settings

logger = logging.getLogger(__name__)


def resolve_server_command(server_command: List[str]) -> List[str]:
    """把裸命令名解析为绝对路径，避免子进程找不到可执行文件。

    例如 ["amap-mcp-server"] → ["D:\\...\\.venv\\Scripts\\amap-mcp-server.exe"]。
    查找顺序：已是路径 → PATH → 项目 .venv/Scripts。
    """
    if not server_command:
        return server_command

    cmd = server_command[0]
    rest = server_command[1:]

    if os.path.sep in cmd or "/" in cmd or "\\" in cmd:
        return server_command

    resolved = shutil.which(cmd)
    if resolved:
        return [resolved] + rest

    # mcp_client.py 位于 backend/app/tools/，向上 3 层到 backend
    backend_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    for venv_dir in (".venv", "venv"):
        script_dir = os.path.join(backend_dir, venv_dir, "Scripts")
        for suffix in (".exe", ".bat", ".cmd", ""):
            candidate = os.path.join(script_dir, cmd + suffix)
            if os.path.isfile(candidate):
                return [candidate] + rest

    return server_command


class MCPClient:
    """MCP stdio 客户端：守护任务持有会话 + 工具发现缓存 + 失败自动重建。

    为什么需要守护任务：anyio 的 cancel scope 要求「谁进入、谁退出」。
    业务请求来自不同 Task，若任一 Task 直接 close 会话，就会踩
    `exit cancel scope in a different task` 运行时错误。
    因此连接的 enter/exit 全部收敛到 _supervise 这一个 Task：
    - 业务侧：_ensure_session 等待 ready 事件拿到 session，直接调用即可
      （session.call_tool 本身是普通 awaitable，跨 Task 调用是安全的）；
    - 关闭/重建：只向守护任务发 close 事件，由它自己退出上下文。
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._session: Optional[Any] = None  # ClientSession，由守护任务写入
        self._tools_cache: Optional[List[Dict[str, Any]]] = None
        # 守护任务与握手事件，懒创建（必须在事件循环内创建）
        self._owner_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._close_event = asyncio.Event()

    # ---------- 可用性 ----------

    @property
    def available(self) -> bool:
        """是否具备启用 MCP 的条件（配置层面，不含运行时连通性）。"""
        if not self.settings.mcp_enabled:
            return False
        # mcp 包未安装时视为不可用（find_spec 只探测不导入，零开销）
        if importlib.util.find_spec("mcp") is None:
            return False
        return bool(self._server_command())

    def _server_command(self) -> List[str]:
        raw = self.settings.mcp_server_command
        try:
            cmd = json.loads(raw) if isinstance(raw, str) else list(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        return resolve_server_command([str(c) for c in cmd])

    def _server_env(self) -> Dict[str, str]:
        """传给 MCP server 的环境变量（如 AMAP_MAPS_API_KEY）。"""
        env: Dict[str, str] = {}
        if self.settings.amap_api_key:
            env["AMAP_MAPS_API_KEY"] = self.settings.amap_api_key
        return env

    # ---------- 会话管理 ----------

    async def _supervise(self) -> None:
        """守护任务：建立会话 → 等关闭信号 → 在本任务内退出上下文。

        进入与退出 AsyncExitStack 都发生在本任务，满足 anyio 的
        same-task 约束。任何启动失败都在这里吞掉并置位 ready，
        让等待方拿到 session=None 后自行走降级路径。
        """
        from mcp import StdioServerParameters
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        command = self._server_command()
        stack = AsyncExitStack()
        try:
            if not command:
                raise RuntimeError("MCP_SERVER_COMMAND 未配置或无法解析")

            env = self._server_env()
            merged = {**os.environ, **env} if env else None
            params = StdioServerParameters(
                command=command[0], args=command[1:], env=merged
            )

            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(
                session.initialize(), timeout=self.settings.mcp_timeout
            )

            self._session = session
            logger.info("MCP 会话已建立：%s", command[0])
        except Exception as exc:
            logger.warning("MCP 会话启动失败：%s", exc)
        finally:
            # 无论成败都通知等待方；等待方通过 self._session 判断结果
            self._ready.set()
            # 持有会话，直到收到关闭信号
            await self._close_event.wait()
            # —— 关闭必须在本任务内完成（same-task 约束）——
            self._session = None
            try:
                await stack.aclose()
            except Exception:
                pass
            logger.info("MCP 会话已关闭：%s", command[0] if command else "?")

    async def _ensure_session(self) -> Any:
        """确保守护任务存活且会话可用，返回 ClientSession。

        会话损坏（调用超时/连接断开）时调用方会先 aclose 再重试，
        这里负责在必要时重新拉起守护任务。
        """
        async with self._lock:
            if self._session is not None:
                return self._session

            # 上一个守护任务可能仍在收尾，等它退出后再重启
            if self._owner_task is not None and not self._owner_task.done():
                self._close_event.set()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._owner_task), timeout=5
                    )
                except (asyncio.TimeoutError, Exception):
                    if not self._owner_task.done():
                        self._owner_task.cancel()

            self._close_event = asyncio.Event()
            self._ready = asyncio.Event()
            self._owner_task = asyncio.create_task(
                self._supervise(), name="mcp-supervisor"
            )
            try:
                await asyncio.wait_for(
                    self._ready.wait(), timeout=self.settings.mcp_timeout + 5
                )
            except asyncio.TimeoutError:
                await self.aclose()
                raise RuntimeError("MCP 会话启动超时")

            if self._session is None:
                raise RuntimeError("MCP 会话启动失败（详见日志）")
            return self._session

    async def aclose(self) -> None:
        """请求关闭会话。实际退出动作由守护任务自己执行（same-task 约束）。"""
        task = self._owner_task
        if task is None or task.done():
            self._session = None
            return
        self._close_event.set()
        try:
            # shield：等待超时也不能取消守护任务——它在 stack.aclose() 的
            # 中途被取消会留下半关闭的 anyio scope，比不关更糟
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            if not task.done():
                task.cancel()
        self._session = None

    # ---------- 工具发现与调用 ----------

    async def list_tools(self) -> List[Dict[str, Any]]:
        """发现 MCP server 提供的全部工具（带缓存）。

        返回 [{name, description, input_schema}]，失败返回空列表。
        """
        if self._tools_cache is not None:
            return self._tools_cache
        if not self.available:
            return []

        try:
            session = await self._ensure_session()
            result = await asyncio.wait_for(
                session.list_tools(), timeout=self.settings.mcp_timeout
            )
            tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": getattr(t, "inputSchema", {}) or {},
                }
                for t in result.tools
            ]
            self._tools_cache = tools
            logger.info("MCP 工具发现完成：%d 个", len(tools))
            return tools
        except Exception as exc:
            logger.warning("MCP 工具发现失败：%s", exc)
            await self.aclose()  # 会话级故障：关闭后下次调用重建
            return []

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用 MCP 工具，返回文本结果。失败时抛出异常，由调用方降级。"""
        if not self.available:
            raise RuntimeError("MCP 不可用")

        session = await self._ensure_session()
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=self.settings.mcp_timeout,
            )
        except Exception:
            # 超时 / 连接断开等会话级故障：关闭会话，下次调用自动重建。
            # 注意不要在其他任务里直接退 anyio 上下文，这里只发信号。
            await self.aclose()
            raise

        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP 工具 {tool_name} 返回错误")

        parts: List[str] = []
        for item in result.content or []:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif hasattr(item, "data"):
                parts.append(str(item.data))
        return "\n".join(parts)

    async def try_call_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Optional[str]:
        """call_tool 的容错版：失败返回 None（含降级日志），不抛异常。"""
        try:
            return await self.call_tool(tool_name, arguments)
        except Exception as exc:
            logger.info("MCP 工具 %s 调用失败（将降级 REST）：%s", tool_name, exc)
            return None


_client: Optional[MCPClient] = None


def get_mcp_client() -> MCPClient:
    """全局单例：会话与工具缓存全程复用。"""
    global _client
    if _client is None:
        _client = MCPClient()
    return _client
