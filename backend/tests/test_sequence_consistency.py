"""景点序号一致性测试。

序号规则（前后端共识，改动必须同步三处）：
  全局连续 —— 第1天 1,2；第2天 3,4；第3天 5,6
  涉及位置：ResultView 列表、store.globalIndex、RouteMap 标记
"""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_result_view_uses_global_index():
    """列表序号必须用 globalIndex，不能退回天内下标 i+1。"""
    view = (FRONTEND / "views" / "ResultView.vue").read_text(encoding="utf-8")
    assert "globalIndex" in view, "列表未使用全局序号"
    # 旧的写法是{{ i + 1 }}，应已不存在
    assert re.search(r"\{\{\s*i\s*\+\s*1\s*\}\}", view) is None, "列表仍在用天内序号"


def test_global_index_is_continuous():
    """验证 store 里的 globalIndex 计算逻辑确实是跨天连续的。"""
    store = (FRONTEND / "stores" / "trip.ts").read_text(encoding="utf-8")

    # globalIndex 在 trip.ts 里定义，ResultView 从 store 取
    assert "globalIndex" in store or "globalIndex" in (
        FRONTEND / "views" / "ResultView.vue"
    ).read_text(encoding="utf-8")

    # 检查是否有跨天累加的 seq 变量（而不是每天重置为 0）
    if "globalIndex" in store:
        assert re.search(r"let\s+seq\s*=\s*0", store), "seq 未初始化"
        assert re.search(r"seq\s*\+=\s*1", store), "seq 未递增"


def test_route_map_uses_global_seq():
    """地图标记序号必须是全局 seq（照片徽章与无图圆点两种形态都要用 s.seq）。"""
    route_map = (FRONTEND / "components" / "charts" / "RouteMap.vue").read_text(
        encoding="utf-8"
    )
    # 照片徽章形态：badge 里渲染 s.seq
    assert '<span class="rm-marker-badge">${s.seq}</span>' in route_map, (
        "照片标记徽章未使用全局序号 s.seq"
    )
    # 无图兜底形态：圆点里渲染 s.seq
    assert '"rm-marker" style="--dot:${color}">${s.seq}</div>' in route_map, (
        "无图标记未使用全局序号 s.seq"
    )
    # 内容模板中绝不能出现 daySeq（zIndex 里的合法使用除外）
    content_part = route_map[
        route_map.index("const content") : route_map.index("const infoWindow")
    ]
    # 去掉 zIndex 那一行后再检查
    without_zindex = "\n".join(
        line for line in content_part.splitlines() if "zIndex" not in line
    )
    assert "daySeq" not in without_zindex, "标记内容误用了天内序号 daySeq"
    # daySeq 只应出现在 zIndex 计算里
    assert "zIndex: 100 - s.daySeq" in route_map, "daySeq 应仅用于 zIndex 排序"


def test_day_seq_only_for_zindex():
    """daySeq 仅用于天内 zIndex 排序，不得出现在标记展示内容里。

    合法位置：类型声明、变量初始化、递增、赋值给 spot、zIndex 计算（共 5 处）。
    """
    route_map = (FRONTEND / "components" / "charts" / "RouteMap.vue").read_text(
        encoding="utf-8"
    )
    uses = re.findall(r"daySeq", route_map)
    assert len(uses) == 5, f"daySeq 出现 {len(uses)} 处（预期 5），可能被误用于展示"

    # 最关键：标记内容里绝不能是 daySeq
    content = re.search(r"rm-marker\"[^>]*>\$\{([^}]+)\}", route_map)
    if content:
        assert content.group(1).strip() == "s.seq", (
            f"标记展示的应是全局序号 s.seq，实际是 {content.group(1)}"
        )


def test_image_url_flows_to_frontend_types():
    """image_url 必须出现在前端类型定义里，否则图片无法展示。"""
    types = (FRONTEND / "types" / "index.ts").read_text(encoding="utf-8")
    assert "image_url" in types, "Attraction 类型缺少 image_url"


def test_image_url_is_https():
    """后端必须把 http 图片升级为 https，否则前端 https 页面会拦截。"""
    amap = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "amap_service.py"
    ).read_text(encoding="utf-8")
    assert 'startswith("http://")' in amap, "未处理 http→https 升级"
    assert "photos" in amap, "未从 POI 的 photos 字段取图片"


def test_route_map_loads_circle_marker_plugin():
    """CircleMarker 需要显式声明插件，否则每日起点圆环不会渲染。"""
    route_map = (FRONTEND / "components" / "charts" / "RouteMap.vue").read_text(
        encoding="utf-8"
    )
    assert "AMap.CircleMarker" in route_map, "未加载 CircleMarker 插件"