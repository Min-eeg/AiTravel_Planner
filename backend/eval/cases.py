"""评测用例集：覆盖正常 / 边界 / 异常场景。

指标由 eval/metrics.py 统一计算，不针对单条用例写死断言，
这样能整体评估 Agent 在各类输入下的生成质量，并便于新增用例。

start_date 统一用固定日期而非"今天"，保证评测结果可复现
（否则不同日期跑出的天气数据不同，无法横向对比）。
"""

from typing import Any, Dict, List

BASE_DATE = "2026-08-10"

EVAL_CASES: List[Dict[str, Any]] = [
    {
        "name": "正常-杭州3天历史文化美食",
        "request": {
            "city": "杭州",
            "start_date": BASE_DATE,
            "days": 3,
            "preferences": ["历史文化", "美食"],
            "budget_level": "中等",
            "pace": "适中",
            "free_text": "",
        },
    },
    {
        "name": "正常-北京4天博物馆深度",
        "request": {
            "city": "北京",
            "start_date": BASE_DATE,
            "days": 4,
            "preferences": ["历史文化", "博物馆"],
            "budget_level": "经济",
            "pace": "紧凑",
            "free_text": "学生党穷游，想多看博物馆，不想早起暴走",
        },
    },
    {
        "name": "边界-单日行程",
        "request": {
            "city": "上海",
            "start_date": BASE_DATE,
            "days": 1,
            "preferences": [],
            "budget_level": "宽松",
            "pace": "紧凑",
            "free_text": "一天玩核心景点",
        },
    },
    {
        "name": "边界-无偏好无备注",
        "request": {
            "city": "成都",
            "start_date": BASE_DATE,
            "days": 3,
            "preferences": [],
            "budget_level": "中等",
            "pace": "适中",
            "free_text": "",
        },
    },
    {
        "name": "边界-最大天数",
        "request": {
            "city": "杭州",
            "start_date": BASE_DATE,
            "days": 15,
            "preferences": ["自然风光", "小众"],
            "budget_level": "经济",
            "pace": "悠闲",
            "free_text": "慢速深度游",
        },
    },
    {
        "name": "异常-未覆盖知识库的城市",
        "request": {
            "city": "拉萨",
            "start_date": BASE_DATE,
            "days": 3,
            "preferences": ["自然风光"],
            "budget_level": "中等",
            "pace": "适中",
            "free_text": "",
        },
    },
    {
        "name": "异常-矛盾约束",
        "request": {
            "city": "北京",
            "start_date": BASE_DATE,
            "days": 2,
            "preferences": ["博物馆", "购物"],
            "budget_level": "经济",
            "pace": "悠闲",
            "free_text": "想住好酒店但预算很紧，想一天暴走又不累",
        },
    },
]