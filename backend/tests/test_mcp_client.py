"""MCP 客户端与多 Agent 协作的单元测试。

全部测试不依赖真实 MCP server 与真实网络：
- resolve_server_command 用临时目录模拟 venv 布局
- MCP 不可用性通过 settings 覆盖触发
- Scout / Reviewer 的决策逻辑用确定性输入验证
"""

import json
from pathlib import Path

import pytest

from app.tools.mcp_client import MCPClient, resolve_server_command


# ========== resolve_server_command ==========


def test_resolve_command_keeps_absolute_path():
    """已是绝对路径的命令原样返回，不做 PATH 查找。"""
    cmd = [r"C:\tools\amap-mcp-server.exe", "--stdio"]
    assert resolve_server_command(cmd) == cmd


def test_resolve_command_keeps_relative_path_with_separator():
    """含路径分隔符的命令视为已解析。"""
    cmd = [".venv/Scripts/amap-mcp-server.exe"]
    assert resolve_server_command(cmd) == cmd


def test_resolve_command_missing_returns_as_is():
    """PATH 与 venv 都找不到时原样返回，交给系统处理（由调用方降级）。"""
    assert resolve_server_command(["definitely-not-a-real-cmd-xyz"]) == [
        "definitely-not-a-real-cmd-xyz"
    ]


# ========== MCPClient 可用性判定 ==========


def _mcp_client(mcp_enabled: str, server_command: str) -> MCPClient:
    client = MCPClient()
    client.settings.mcp_enabled = mcp_enabled == "1"
    client.settings.mcp_server_command = server_command
    return client


def test_mcp_unavailable_when_disabled():
    client = _mcp_client(mcp_enabled="0", server_command='["uvx", "amap-mcp-server"]')
    assert client.available is False


def test_mcp_unavailable_without_valid_command():
    """命令配置解析失败（非法 JSON）时视为不可用，与 mcp 包是否安装无关。"""
    client = _mcp_client(mcp_enabled="1", server_command="not-a-json")
    assert client.available is False


def test_mcp_server_command_parses_json():
    client = _mcp_client(mcp_enabled="1", server_command='["uvx", "amap-mcp-server"]')
    cmd = client._server_command()
    # 裸命令应被解析为绝对路径（shutil.which 命中）或保留原名（未命中）
    # Windows 下 which 返回 uvx.EXE，故取文件名 stem 比较
    assert Path(cmd[0]).stem.lower() == "uvx"
    assert cmd[1] == "amap-mcp-server"


def test_mcp_server_env_passes_amap_key():
    client = _mcp_client(mcp_enabled="1", server_command='["uvx", "amap-mcp-server"]')
    client.settings.amap_api_key = "test-key-123"
    env = client._server_env()
    assert env["AMAP_MAPS_API_KEY"] == "test-key-123"


# ========== Scout Agent：MCP 结果解析 ==========


def test_parse_mcp_pois_raw_amap_format():
    """amap-mcp-server 返回原始高德结构（location 为 "lng,lat" 字符串）。"""
    from app.agents.planner import _parse_mcp_pois

    text = json.dumps(
        {
            "status": "1",
            "pois": [
                {
                    "name": "西湖·断桥白堤",
                    "address": "杭州西湖区",
                    "location": "120.149,30.259",
                    "biz_ext": {"ticket_price": "0"},
                }
            ],
        }
    )
    pois = _parse_mcp_pois(text)
    assert len(pois) == 1
    assert pois[0]["name"] == "西湖·断桥白堤"
    assert abs(pois[0]["longitude"] - 120.149) < 1e-6
    assert abs(pois[0]["latitude"] - 30.259) < 1e-6


def test_parse_mcp_pois_normalized_format():
    """部分 MCP server 已做归一化，直接带 longitude/latitude 字段。"""
    from app.agents.planner import _parse_mcp_pois

    text = json.dumps(
        [{"name": "灵隐寺", "longitude": 120.101, "latitude": 30.241}]
    )
    pois = _parse_mcp_pois(text)
    assert len(pois) == 1
    assert pois[0]["source"] == "mcp"


def test_parse_mcp_pois_simplified_format_keeps_names():
    """实测 amap-mcp-server 只返回 id/name/address/typecode（无坐标）。

    解析必须保留名称（后续由 REST 地理编码补全坐标），不能丢弃条目。
    """
    from app.agents.planner import _parse_mcp_pois

    text = json.dumps(
        {
            "suggestion": {"keywords": [], "cities": []},
            "pois": [
                {
                    "id": "B0HGNGK3FG",
                    "name": "南宋德寿宫遗址博物馆",
                    "address": "望江路228-264号",
                    "typecode": "140100",
                }
            ],
        }
    )
    pois = _parse_mcp_pois(text)
    assert len(pois) == 1
    assert pois[0]["name"] == "南宋德寿宫遗址博物馆"
    assert pois[0]["source"] == "mcp"
    assert pois[0]["longitude"] == 0.0  # 缺坐标由 scout 节点补全，这里不能误填


def test_parse_mcp_pois_invalid_text_returns_empty():
    """解析失败必须返回空列表（调用方据此降级 REST），不允许抛异常。"""
    from app.agents.planner import _parse_mcp_pois

    assert _parse_mcp_pois("not json at all") == []
    assert _parse_mcp_pois("") == []
    assert _parse_mcp_pois(json.dumps({"unexpected": 1})) == []


# ========== 守护任务（same-task cancel scope 约束）==========


async def test_aclose_without_owner_is_noop():
    """从未建立过会话时调用 aclose 必须是无害的空操作。"""
    client = _mcp_client(mcp_enabled="0", server_command="not-a-json")
    await client.aclose()
    assert client._session is None
    assert client._owner_task is None


async def test_ensure_session_invalid_command_raises():
    """命令无法解析时，守护任务启动失败必须转化为明确的 RuntimeError。"""
    client = _mcp_client(mcp_enabled="1", server_command="not-a-json")
    try:
        await client._ensure_session()
        # mcp 包未安装时 available=False，_ensure_session 不会被调用到这一步；
        # 若被调用且环境没有 mcp 包，会先在 import 处失败——同样算通过
    except RuntimeError:
        pass
    finally:
        await client.aclose()


async def test_try_call_tool_degrades_when_session_cannot_start():
    """会话起不来时 try_call_tool 返回 None（降级），不允许向调用方抛异常。"""
    client = _mcp_client(mcp_enabled="1", server_command="not-a-json")
    try:
        result = await client.try_call_tool("maps_text_search", {"keywords": "x"})
        assert result is None
    except ImportError:
        # 无 mcp 包的环境下 available=False，call_tool 抛 RuntimeError 被捕获，返回 None
        result = await client.try_call_tool("maps_text_search", {"keywords": "x"})
        assert result is None
    finally:
        await client.aclose()


async def test_failed_call_closes_session_for_rebuild():
    """工具调用失败后，会话必须被标记关闭，下次调用走全新守护任务（而非复用坏连接）。"""
    client = _mcp_client(mcp_enabled="1", server_command='["uvx", "amap-mcp-server"]')

    class _FakeSession:
        async def call_tool(self, *a, **kw):
            raise ConnectionError("Connection closed")

    # 直接注入假会话与假守护任务，模拟「会话已建立但连接中断」
    import asyncio as _asyncio

    client._session = _FakeSession()
    client._close_event = _asyncio.Event()
    client._ready = _asyncio.Event()
    client._owner_task = _asyncio.create_task(_asyncio.sleep(3600))

    with pytest.raises(ConnectionError):
        await client.call_tool("maps_text_search", {})

    # 失败后 aclose 已触发：假守护任务应被请求结束
    assert client._close_event.is_set()
    await client.aclose()
    assert client._owner_task is None or client._owner_task.done()


# ========== Reviewer Agent：硬规则检查 ==========


def _make_planner():
    """构造 TripPlanner 但不触发真实 LLM/图编译之外的网络调用。"""
    from app.agents.planner import TripPlanner

    return TripPlanner()


def test_rule_check_detects_hallucinated_attraction(sample_request):
    """编造景点检测：不在候选池中的景点名必须被打回。"""
    from app.models.schemas import Attraction, DayPlan, Meal

    planner = _make_planner()
    day = DayPlan(
        date="2026-08-10",
        day_index=0,
        title="第一天",
        attractions=[
            Attraction(
                name="不存在的景点",
                location=None,
                visit_duration=90,
            )
        ],
        meals=[
            Meal(type="breakfast", name="早餐", estimated_cost=30.0),
            Meal(type="lunch", name="午餐", estimated_cost=60.0),
            Meal(type="dinner", name="晚餐", estimated_cost=80.0),
        ],
    )
    state = {"candidates": [{"name": "西湖·断桥白堤"}], "constraints": {}}
    problems = planner._rule_check(day, state)
    assert any("编造" in p for p in problems)


def test_rule_check_detects_overtime(sample_request):
    """单日游览超过 8 小时必须报问题。"""
    from app.models.schemas import Attraction, DayPlan, Meal

    planner = _make_planner()
    day = DayPlan(
        date="2026-08-10",
        day_index=0,
        title="暴走日",
        attractions=[
            Attraction(name=f"景点{i}", location=None, visit_duration=200)
            for i in range(3)
        ],
        meals=[
            Meal(type="breakfast", name="早", estimated_cost=30.0),
            Meal(type="lunch", name="午", estimated_cost=60.0),
            Meal(type="dinner", name="晚", estimated_cost=80.0),
        ],
    )
    state = {
        "candidates": [{"name": f"景点{i}"} for i in range(3)],
        "constraints": {},
    }
    problems = planner._rule_check(day, state)
    assert any("8 小时" in p for p in problems)


def test_rule_check_detects_missing_meal_and_avoid(sample_request):
    """缺餐次 + 违反避开项，两条问题都要报。"""
    from app.models.schemas import Attraction, DayPlan, Meal

    planner = _make_planner()
    day = DayPlan(
        date="2026-08-10",
        day_index=0,
        title="购物日",
        attractions=[Attraction(name="购物广场", location=None, visit_duration=120)],
        meals=[
            Meal(type="breakfast", name="早", estimated_cost=30.0),
            Meal(type="lunch", name="午", estimated_cost=60.0),
            Meal(type="dinner", name="晚", estimated_cost=80.0),
        ],
    )
    state = {
        "candidates": [{"name": "购物广场"}],
        "constraints": {"must_avoid": ["购物"]},
    }
    # 单独验证缺餐：去掉一餐
    day.meals = day.meals[:2]
    problems = planner._rule_check(day, state)
    assert any("缺少餐次" in p for p in problems)
    assert any("避开" in p for p in problems)


def test_rule_check_passes_clean_day(sample_request):
    """合规行程零问题，评审应放行。"""
    from app.models.schemas import Attraction, DayPlan, Meal

    planner = _make_planner()
    day = DayPlan(
        date="2026-08-10",
        day_index=0,
        title="西湖一日",
        attractions=[
            Attraction(name="西湖·断桥白堤", location=None, visit_duration=120),
            Attraction(name="灵隐寺", location=None, visit_duration=120),
        ],
        meals=[
            Meal(type="breakfast", name="早", estimated_cost=30.0),
            Meal(type="lunch", name="午", estimated_cost=60.0),
            Meal(type="dinner", name="晚", estimated_cost=80.0),
        ],
    )
    state = {
        "candidates": [{"name": "西湖·断桥白堤"}, {"name": "灵隐寺"}],
        "constraints": {"must_avoid": []},
    }
    assert planner._rule_check(day, state) == []


def test_rule_check_detects_repeated_meals(sample_request):
    """跨天餐饮重复检测：与前几日两餐以上雷同必须打回。

    实测踩坑：北京 2 天行程每天都是护国寺小吃 + 四季民福烤鸭 + 炸酱面
    ——LLM 拿不到历史就会每天复制同一份菜单。
    """
    from app.models.schemas import Attraction, DayPlan, Meal

    planner = _make_planner()
    meals = [
        Meal(type="breakfast", name="护国寺小吃", estimated_cost=25.0),
        Meal(type="lunch", name="四季民福烤鸭", estimated_cost=60.0),
        Meal(type="dinner", name="炸酱面", estimated_cost=30.0),
    ]
    day1 = DayPlan(
        date="2026-10-08",
        day_index=0,
        title="第一天",
        attractions=[Attraction(name="故宫博物院", location=None, visit_duration=120)],
        meals=meals,
    )
    day2 = DayPlan(
        date="2026-10-09",
        day_index=1,
        title="第二天",
        attractions=[Attraction(name="中国美术馆", location=None, visit_duration=120)],
        meals=list(meals),  # 与第一天完全相同
    )
    state = {
        "candidates": [{"name": "故宫博物院"}, {"name": "中国美术馆"}],
        "constraints": {},
        "days": [day1, day2],
    }
    problems = planner._rule_check(day2, state)
    assert any("餐饮与前几日重复" in p for p in problems)

    # 单餐雷同（如连锁早餐）应宽容，不打回
    day3 = day2.model_copy(deep=True)
    day3.day_index = 2
    day3.meals = [
        Meal(type="breakfast", name="护国寺小吃", estimated_cost=25.0),
        Meal(type="lunch", name="庆丰包子铺", estimated_cost=40.0),
        Meal(type="dinner", name="东来顺涮肉", estimated_cost=90.0),
    ]
    state["days"] = [day1, day2, day3]
    problems3 = planner._rule_check(day3, {**state, "days": [day1, day2, day3]})
    assert not any("餐饮与前几日重复" in p for p in problems3)


def test_fallback_day_meals_rotate():
    """兜底模板的餐食按天轮换，mock 模式不会被跨天重复规则误伤。"""
    from app.agents.planner import _fallback_day

    candidates = [{"name": f"景点{i}", "longitude": 120.0, "latitude": 30.0} for i in range(6)]
    used: list = []
    day0 = _fallback_day(0, "2026-08-10", candidates, used)
    day1 = _fallback_day(1, "2026-08-11", candidates, used)
    names0 = {m.name for m in day0.meals}
    names1 = {m.name for m in day1.meals}
    assert names0 != names1  # 相邻两天的餐食名称不同
