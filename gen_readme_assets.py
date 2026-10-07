"""生成 README 用的架构图 PNG（assets/showcase/）。

用 matplotlib 矢量绘制后导出 PNG（GitHub README 引用本地图片必须是位图文件）。
运行：python gen_readme_assets.py（在项目根目录执行）
"""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from pathlib import Path

plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False

OUT = Path("assets/showcase")
OUT.mkdir(parents=True, exist_ok=True)

BLUE = "#378ADD"
GREEN = "#1D9E75"
ORANGE = "#EF9F27"
PURPLE = "#7F77DD"
PINK = "#D4537E"
INK = "#2C2C2A"
GREY = "#888780"
LINE = "#D3D1C7"


def box(ax, x, y, w, h, text, color, fontsize=11, text_color="white", lw=0):
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h,
            boxstyle="round,pad=0.02,rounding_size=0.06",
            facecolor=color, edgecolor=color, linewidth=lw,
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fontsize, color=text_color, weight="medium")


def arrow(ax, x1, y1, x2, y2, color=GREY, style="-|>", lw=1.6, ls="-"):
    ax.add_patch(FancyArrowPatch(
        (x1, y1), (x2, y2), arrowstyle=style, mutation_scale=14,
        color=color, linewidth=lw, linestyle=ls, shrinkA=2, shrinkB=2,
    ))


# ============ 1. 系统架构图 ============
fig, ax = plt.subplots(figsize=(11, 6.2), dpi=160)
ax.set_xlim(0, 11)
ax.set_ylim(0, 6.2)
ax.axis("off")

# 前端层
box(ax, 0.4, 4.9, 3.0, 0.85, "Vue 3 + TypeScript + Pinia", BLUE)
ax.text(1.9, 6.0, "前端", ha="center", fontsize=12, color=INK, weight="bold")
ax.text(1.9, 4.55, "SSE 流式渲染 · 高德地图\n天气卡片 · 预算分解", ha="center", fontsize=8.5, color=GREY)

# 后端 API 层
box(ax, 4.4, 4.9, 2.6, 0.85, "FastAPI\nSSE 端点 / REST", GREEN)
ax.text(5.7, 6.0, "后端", ha="center", fontsize=12, color=INK, weight="bold")

# Agent 编排层
box(ax, 3.95, 3.1, 3.5, 0.85, "LangGraph 编排\nparse → retrieve → plan_day×N → finalize", PURPLE, fontsize=8.5)
ax.text(5.7, 2.75, "结构化输出（中文 schema）+ 三段容错链", ha="center", fontsize=8.5, color=GREY)

# 服务层
box(ax, 0.4, 1.3, 1.75, 0.85, "LLM 服务\nqwen-max", ORANGE, fontsize=9)
box(ax, 2.45, 1.3, 1.75, 0.85, "高德服务\nPOI / 地理编码", BLUE, fontsize=9)
box(ax, 4.5, 1.3, 1.75, 0.85, "天气服务\nOpen-Meteo", GREEN, fontsize=9)
box(ax, 6.55, 1.3, 1.75, 0.85, "RAG 检索\nBM25", PINK, fontsize=9)
ax.text(4.35, 0.95, "服务层（可插拔缓存）", ha="center", fontsize=10, color=INK, weight="bold")

# 数据层
box(ax, 8.8, 3.1, 1.8, 0.85, "SQLite\n行程历史", "#8A8A85", fontsize=9)
box(ax, 8.8, 1.3, 1.8, 0.85, "知识库\nMarkdown", "#B0AFA8", fontsize=9)

# 连线
arrow(ax, 3.45, 5.32, 4.35, 5.32, INK)           # 前端 → 后端
arrow(ax, 5.7, 4.85, 5.7, 4.0, INK)              # 后端 → LangGraph
arrow(ax, 4.8, 3.05, 1.35, 2.2, ORANGE)          # LangGraph → LLM
arrow(ax, 5.3, 3.05, 3.3, 2.2, BLUE)             # → 高德
arrow(ax, 5.75, 3.05, 5.4, 2.2, GREEN)           # → 天气
arrow(ax, 6.3, 3.05, 7.4, 2.2, PINK)             # → RAG
arrow(ax, 7.3, 3.5, 8.75, 3.5, "#8A8A85")        # LangGraph → SQLite
arrow(ax, 7.42, 1.7, 8.75, 1.7, "#B0AFA8")       # RAG → 知识库
arrow(ax, 3.3, 2.2, 3.3, 2.2)                    # 占位

fig.savefig(OUT / "architecture.png", bbox_inches="tight", facecolor="white")
plt.close(fig)

# ============ 2. LangGraph 编排流程 ============
fig, ax = plt.subplots(figsize=(11.5, 3.4), dpi=160)
ax.set_xlim(0, 11.5)
ax.set_ylim(0, 3.4)
ax.axis("off")

steps = [
    ("START", "#B0AFA8"),
    ("parse_input\n约束解析", BLUE),
    ("retrieve\nPOI ∥ 知识库", GREEN),
    ("plan_day ×N\n逐日生成", ORANGE),
    ("correct_geo\n坐标校正", PURPLE),
    ("finalize\n预算结算", PINK),
    ("END", "#B0AFA8"),
]
w, h, gap = 1.42, 0.95, 0.28
x = 0.15
centers = []
for label, color in steps:
    light = color == "#B0AFA8"
    box(ax, x, 1.35, w, h, label, color, fontsize=9,
        text_color=INK if light else "white")
    centers.append(x + w / 2)
    x += w + gap

for i in range(len(steps) - 1):
    arrow(ax, centers[i] + w / 2, 1.82, centers[i + 1] - w / 2, 1.82, INK)

# 兜底链
box(ax, 4.3, 0.25, 2.9, 0.7, "fallback 模板兜底\n（degraded=true）", "#F3E3D3",
    fontsize=9, text_color="#8A5A0B")
arrow(ax, 5.75, 1.3, 5.75, 1.0, PINK, ls="--")
ax.text(9.55, 0.6, "解析失败 / 超时 → 重试 →\n模板兜底，绝不白屏",
        fontsize=8.5, color=GREY, ha="center", va="center")

fig.savefig(OUT / "langgraph.png", bbox_inches="tight", facecolor="white")
plt.close(fig)

# ============ 3. SSE 事件流 ============
fig, ax = plt.subplots(figsize=(10.5, 4.6), dpi=160)
ax.set_xlim(0, 10.5)
ax.set_ylim(0, 4.6)
ax.axis("off")

# 左右两端：后端发出 → 前端接收
box(ax, 0.35, 2.0, 2.1, 0.8, "后端\nLangGraph 逐日生成", GREEN, fontsize=9.5)
box(ax, 8.05, 2.0, 2.1, 0.8, "前端\nPinia 增量渲染", BLUE, fontsize=9.5)

# 中央垂直时间线
TL = 5.25
ax.plot([TL, TL], [0.45, 4.05], color=LINE, lw=2)
arrow(ax, TL, 4.05, TL, 4.35, GREY)
ax.text(TL, 4.42, "时间", fontsize=9, color=GREY, ha="center")

events = [
    ("meta", "行程元信息（城市/日期/天数）", BLUE),
    ("trace ×N", "节点执行轨迹（含耗时）", "#9A9A95"),
    ("day", "第 1 天行程 + 景点照片，先到先渲染", ORANGE),
    ("day", "第 2 天…逐日下发", ORANGE),
    ("chart", "结算快照（预算/天气/逐日住宿交通）", GREEN),
    ("done", "汇总（耗时/降级标记）", PURPLE),
]
y = 3.75
for label, desc, color in events:
    ax.plot([TL - 0.12, TL], [y, y], color=color, lw=2)
    box(ax, 3.15, y - 0.19, 1.35, 0.4, label, color, fontsize=9)
    ax.text(4.7, y, desc, fontsize=9.5, color=INK, va="center")
    y -= 0.62

# 后端 → 事件线；事件线 → 前端
arrow(ax, 2.5, 2.4, 3.1, 2.4, GREEN, lw=1.8)
arrow(ax, 7.4, 2.4, 8.0, 2.4, BLUE, lw=1.8)
ax.text(9.1, 1.25, "POST /api/trip/stream\ntext/event-stream",
        fontsize=8.5, color=GREY, ha="center", va="top")

fig.savefig(OUT / "sse-events.png", bbox_inches="tight", facecolor="white")
plt.close(fig)

print("生成完成：")
for f in sorted(OUT.glob("*.png")):
    print(" -", f, f"({f.stat().st_size // 1024} KB)")