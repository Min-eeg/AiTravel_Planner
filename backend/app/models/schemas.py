"""数据模型：Pydantic schemas，定义请求 / 行程结构 / 流式事件契约。

这里的结构是前后端共享的契约，改动需同步 frontend/src/types/index.ts。
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ============ 请求 ============


class TripRequest(BaseModel):
    """行程生成请求。"""

    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期 YYYY-MM-DD")
    days: int = Field(..., ge=1, le=15, description="旅行天数")
    preferences: List[str] = Field(default_factory=list, description="偏好标签")
    budget_level: str = Field(default="中等", description="预算档位：经济/中等/宽松")
    pace: str = Field(default="适中", description="节奏：紧凑/适中/悠闲")
    free_text: str = Field(default="", description="自由文本额外要求")


# ============ 行程结构 ============


class Location(BaseModel):
    longitude: float
    latitude: float


class Attraction(BaseModel):
    name: str
    address: str = ""
    location: Optional[Location] = None
    visit_duration: int = 120
    description: str = ""
    category: str = "景点"
    ticket_price: float = 0.0
    image_url: Optional[str] = None
    # 是否由坐标校正覆盖了 LLM 的编造值
    location_corrected: bool = False


class Meal(BaseModel):
    type: str  # breakfast / lunch / dinner
    name: str
    description: str = ""
    estimated_cost: float = 0.0


class Hotel(BaseModel):
    name: str
    address: str = ""
    price_range: str = ""
    estimated_cost: float = 0.0


class DayPlan(BaseModel):
    date: str
    day_index: int
    title: str = ""
    description: str = ""
    transportation: str = ""
    # 市内交通估算（由 settle_budget 按预算档位写回，前端逐日展示）
    transportation_cost: float = 0.0
    hotel: Optional[Hotel] = None
    attractions: List[Attraction] = Field(default_factory=list)
    meals: List[Meal] = Field(default_factory=list)


class WeatherInfo(BaseModel):
    date: str
    day_weather: str = ""
    night_weather: str = ""
    day_temp: float = 0.0
    night_temp: float = 0.0
    # 降水概率（0-100）。比温度更能回答"要不要带伞"，
    # Open-Meteo 提供；高德天气不提供时为 None
    precipitation_prob: Optional[int] = None


class Budget(BaseModel):
    total_attractions: float = 0.0
    total_hotels: float = 0.0
    total_meals: float = 0.0
    total_transportation: float = 0.0
    total: float = 0.0


class TripPlan(BaseModel):
    city: str
    start_date: str
    days: List[DayPlan] = Field(default_factory=list)
    weather: List[WeatherInfo] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    overall_suggestions: str = ""
    # 该行程是否由降级兜底链路产生（前端需要明显标注）
    degraded: bool = False


# ============ 流式事件契约 ============


class TraceStep(BaseModel):
    """工具调用 / Agent 步骤的可观测记录。"""

    stage: str
    name: str
    status: str = "running"  # running / ok / failed / cached
    duration_ms: Optional[int] = None
    detail: str = ""


class StreamMeta(BaseModel):
    """meta 事件：立即下发的骨架，让前端秒渲染框架。"""

    city: str
    start_date: str
    days: int
    request_id: str
    preferences: List[str] = Field(default_factory=list)


class StreamDay(BaseModel):
    """day 事件：一天生成完毕即可渲染。"""

    day: DayPlan


class StreamPatch(BaseModel):
    """patch 事件：增量修正，不重发已渲染内容。"""

    day_index: int
    attraction_index: int
    location: Location
    corrected: bool = True


class StreamChart(BaseModel):
    """chart 事件：结算完成的数据快照。

    days 在 settle_budget 之后下发（含逐日住宿/交通），
    前端收到后覆盖已渲染的逐日卡片——流式的 day 事件先到、
    结算字段后补，两段拼成完整数据。
    """

    budget: Budget
    weather: List[WeatherInfo]
    days: List[DayPlan] = Field(default_factory=list)


class StreamDone(BaseModel):
    """done 事件：生成收尾的汇总报告。"""

    total_duration_ms: int
    days_generated: int
    attractions_count: int
    corrected_count: int
    degraded: bool
    llm_calls: int = 0


class StreamError(BaseModel):
    """error 事件：降级为模板计划，仍返回可用行程，绝不白屏。"""

    message: str
    stage: str = ""
    degraded: bool = True