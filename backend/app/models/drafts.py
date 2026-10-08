"""LangGraph 编排用结构化模型。

这些 Pydantic 模型直接作为 LangChain `with_structured_output` 的 schema，
让 LLM 输出天然符合结构，而不是靠提示词祈祷它返回合法 JSON。

⚠️ 为什么每个字段都要写 `alias`（这是踩坑后的必要设计）：
实测中文模型（qwen-max）在 schema 字段名为英文时，会按自己的语义理解
输出中文键名，例如把 `attractions` 写成 `景点`、`meals` 写成 `餐饮`，
导致 schema 校验失败、整份内容被丢弃降级为模板。
显式给出中文 alias 后，schema 的 property 名就是中文，模型输出与 schema 对齐。
注意：`with_structured_output` 走的是「校验/解析」而非「序列化」，
因此 alias 直接作为 JSON key 生效，`populate_by_name` 用于兼容两种输入。
"""

from typing import Any, Dict, List, Union

from pydantic import BaseModel, ConfigDict, Field


class _DraftBase(BaseModel):
    """所有草稿模型的公共配置。"""

    model_config = ConfigDict(
        populate_by_name=True,  # 允许用字段名或 alias 两种方式填充
        extra="ignore",  # 模型多给字段时忽略，避免整体校验失败
    )


class AttractionDraft(_DraftBase):
    """LLM 生成的景点草稿。

    注意这里不包含 location：经纬度一律由后端从 POI 候选池回查注入，
    因为 LLM 给的坐标基本都是编造的。

    字段描述要写清单位——实测模型会把「游览 2 小时」填进 visit_duration，
    单位不明确时它会按自己的理解填，产出的数值毫无意义。
    """

    name: str = Field(
        ...,
        alias="景点名称",
        description="景点名称，必须与「可用候选」中的名称完全一致，严禁改写或编造",
    )
    visit_duration: int = Field(
        default=120,
        alias="游览分钟",
        description="建议游览时长，单位：分钟，必须是整数。例��� 90 表示 1.5 小时。"
        "不要填小时数（如填 2 表示 2 小时会被理解成 2 分钟）",
    )
    description: str = Field(
        default="", alias="看点介绍", description="这个景点值得看什么，40 字以内"
    )
    category: str = Field(
        default="景点", alias="类别", description="景点类别，如文化古迹 / 自然风光 / 博物馆"
    )


class MealDraft(_DraftBase):
    """餐饮草稿。"""

    type: str = Field(
        ...,
        alias="餐次",
        description="餐次类型，只能是 breakfast（早餐）/ lunch（午餐）/ dinner（晚餐）",
    )
    name: str = Field(default="", alias="餐名", description="推荐的具体餐食或店名")
    description: str = Field(
        default="", alias="特色说明", description="本地特色、推荐理由，30 字以内"
    )
    estimated_cost: float = Field(
        default=0.0,
        alias="预估人均消费",
        description="预估人均消费，单位：元，必须是数字。例��� 50 表示 50 元，不要填单位",
    )


class DayDraft(_DraftBase):
    """单日行程草稿。

    `餐饮安排` 用 Union 兼容两种形态：中文模型有时会误以为三餐是固定三项，
    输出成 `{"breakfast": "...", "lunch": "...", "dinner": "..."}` 这样的对象
    而不是数组。两种形态都接受，避免整份内容因结构误判被丢弃。
    """

    title: str = Field(default="", alias="当天主题", description="当天行程主题，20 字以内")
    description: str = Field(
        default="", alias="当天概述", description="当天行程概述，50 字以内"
    )
    attractions: List[AttractionDraft] = Field(
        ...,
        alias="景点安排",
        description="当天安排的景点，必须是数组，2-3 个，地理邻近。"
        "格式示例：[{\"景点名称\": \"西湖\", \"游览分钟\": 90}]",
    )
    meals: Union[List[MealDraft], Dict[str, Any]] = Field(
        ...,
        alias="餐饮安排",
        description="当天餐饮，必须是数组，恰好三项 breakfast/lunch/dinner。"
        '格式示例：[{"餐次": "breakfast", "餐名": "片儿川", "预估人均消费": 25}]',
    )

    def normalized_meals(self) -> List[MealDraft]:
        """把两种形态的餐饮统一为 MealDraft 列表。

        对象形态（模型误输出）时按 breakfast/lunch/dinner 键还原。
        """
        if isinstance(self.meals, list):
            return self.meals

        mapping = {
            "breakfast": "breakfast",
            "lunch": "lunch",
            "dinner": "dinner",
        }
        result: List[MealDraft] = []
        for key, name in mapping.items():
            raw = self.meals.get(key)
            if raw is None:
                continue
            try:
                if isinstance(raw, str):
                    result.append(MealDraft(type=name, **{"餐名": raw}))
                elif isinstance(raw, dict):
                    # 模型可能用英文键或中文键，这里统一过滤掉 None 再交给 Pydantic
                    payload = {k: v for k, v in raw.items() if v is not None}
                    payload.setdefault("餐次", name)
                    payload.setdefault("type", name)
                    result.append(MealDraft.model_validate(payload))
            except Exception:
                # 单项解析失败就跳过这一项，三餐补齐逻辑会兜住
                continue
        return result


class ConstraintsDraft(_DraftBase):
    """Planner 阶段从自由输入中抽取的结构化约束。"""

    pace: str = Field(
        default="适中", alias="行程节奏", description="节奏：紧凑 / 适中 / 悠闲"
    )
    budget_level: str = Field(
        default="中等", alias="预算档位", description="预算档位：经济 / 中等 / 宽松"
    )
    interests: List[str] = Field(
        default_factory=list,
        alias="兴趣标签",
        description="从自由文本中抽出的兴趣点，如博物馆、美食、自然风光",
    )
    must_avoid: List[str] = Field(
        default_factory=list, alias="需要避开", description="用户明确排斥的内容"
    )
    notes: str = Field(
        default="", alias="其他约束", description="其他需要遵守的约束，没有则留空"
    )


class SearchPlanDraft(_DraftBase):
    """Scout Agent（景点侦察）的检索计划。

    多智能体分工：Scout 不自己找景点，而是决定「用什么关键词去调工具」——
    这是 Agent 的决策输出，真正的搜索由 MCP 工具 / REST 直连执行。
    """

    queries: List[str] = Field(
        ...,
        alias="搜索关键词",
        description=(
            "3-5 组高德 POI 搜索关键词，每组覆盖一个兴趣维度，"
            '按优先级排序。格式示例：["博物馆", "历史古迹", "特色美食街"]'
        ),
    )


class ReviewDraft(_DraftBase):
    """Reviewer Agent（行程评审）的单日质检结论。"""

    passed: bool = Field(
        ...,
        alias="是否合格",
        description="当天行程是否合格，合格为 true",
    )
    problems: List[str] = Field(
        default_factory=list,
        alias="问题列表",
        description="发现的问题，每条一句话，如「景点 A 与 B 相距过远」",
    )
    suggestion: str = Field(
        default="",
        alias="修改建议",
        description="给规划 Agent 的修改建议，50 字以内",
    )