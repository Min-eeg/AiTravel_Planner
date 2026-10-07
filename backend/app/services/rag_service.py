"""RAG 知识库检索：让行程带"本地玩法经验"，而不只是景点罗列。

技术选型（为什么不自己写 BM25）：
- 打分算法用 `rank_bm25.BM25Okapi` —— 这是最成熟的 Python BM25 实现，
  自己实现既没优势又难维护
- 分词用 `jieba`（可选依赖，缺失时退化为二元组切分），中文检索效果明显更好
- 检索器封装为 LangChain 的 `BaseRetriever` 子类，因此可以直接
  `.invoke(query)`，也能无缝换成向量检索

知识库格式：backend/data/kb/{城市}旅行知识库.md
新增城市只需放一个 md 文件，无需改任何代码（刻意设计：让扩充知识库成为纯数据工作）。
"""

import logging
from pathlib import Path
from typing import List, Optional

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_text_splitters import MarkdownHeaderTextSplitter

from ..core.config import get_settings

logger = logging.getLogger(__name__)

KB_DIR = Path(__file__).resolve().parents[2] / "data" / "kb"

_HEADERS = [("##", "h2"), ("###", "h3")]

# jieba 是可选依赖：装了用 jieba 分词，效果更好；没装则退化为二元组
try:
    import jieba

    _HAS_JIEBA = True
except ImportError:  # pragma: no cover - 取决于环境
    jieba = None
    _HAS_JIEBA = False


def _tokenize(text: str) -> List[str]:
    """分词：有 jieba 用 jieba，否则按二元组切分。

    二元组是零依赖兜底方案：中文查询词与文档片段也能部分匹配。
    """
    lowered = text.lower()
    if _HAS_JIEBA:
        return [t for t in jieba.cut(lowered) if t.strip()]

    import re

    tokens: List[str] = []
    for match in re.findall(r"[a-z0-9]+|[一-鿿]+", lowered):
        if re.fullmatch(r"[一-鿿]+", match):
            if len(match) == 1:
                tokens.append(match)
            else:
                tokens.extend(match[i : i + 2] for i in range(len(match) - 1))
        else:
            tokens.append(match)
    return tokens


class BM25Retriever(BaseRetriever):
    """基于 rank_bm25 的关键词检索器。

    封装成 LangChain Retriever 接口的理由：业务代码只依赖 `.invoke()`，
    未来要换向量检索只需替换这个类，上层调用方零改动。
    """

    docs: List[Document]
    k: int = 4

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None
    ) -> List[Document]:
        from rank_bm25 import BM25Okapi

        if not self.docs:
            return []

        corpus_tokens = [_tokenize(d.page_content) for d in self.docs]
        # 空语料会让 BM25Okapi 除零，必须过滤
        corpus_tokens = [t if t else ["_"] for t in corpus_tokens]

        bm25 = BM25Okapi(corpus_tokens)
        scores = bm25.get_scores(_tokenize(query))

        # 全零分说明查询与文档无重叠，此时返回空比返回任意 top-k 更合理
        if not any(scores):
            return []

        ranked = sorted(zip(self.docs, scores), key=lambda x: x[1], reverse=True)
        return [doc for doc, score in ranked[: self.k] if score > 0]


class CityKnowledgeBase:
    """城市知识库。带索引缓存，避免每次请求重新分词建索引。"""

    def __init__(self) -> None:
        self._splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=_HEADERS, strip_headers=False
        )
        self._index: dict[str, tuple[List[Document], str]] = {}

    def _load_file(self, city: str) -> Optional[Path]:
        if not KB_DIR.is_dir():
            return None
        for path in sorted(KB_DIR.glob("*.md")):
            if city in path.stem:
                return path
        return None

    def _build(self, city: str) -> Optional[tuple[List[Document], str]]:
        if city in self._index:
            return self._index[city]

        path = self._load_file(city)
        if path is None:
            logger.info("未找到城市知识库：%s", city)
            return None

        try:
            md_text = path.read_text(encoding="utf-8")
            docs: List[Document] = self._splitter.split_text(md_text)
            # 过短的片段（仅剩标题）会稀释检索质量
            docs = [d for d in docs if len(d.page_content.strip()) > 40]
            if not docs:
                return None
            self._index[city] = (docs, md_text)
            logger.info("已建立知识库索引：%s（%s 个片段）", city, len(docs))
            return self._index[city]
        except Exception as exc:
            logger.warning("知识库索引构建失败 %s：%s", city, exc)
            return None

    def search(self, city: str, query: str, top_k: int = 4) -> str:
        """检索并返回拼接文本，供注入提示词。无知识库时返回空串。"""
        if not get_settings().rag_enabled:
            return ""

        built = self._build(city)
        if built is None:
            return ""
        docs, _ = built

        try:
            hits = BM25Retriever(docs=docs, k=top_k).invoke(query)
        except Exception as exc:
            logger.warning("知识库检索失败 %s：%s", city, exc)
            return ""

        if not hits:
            return ""

        parts = []
        for doc in hits:
            title = doc.metadata.get("h2") or doc.metadata.get("h3") or "知识"
            parts.append(f"【{title}】\n{doc.page_content.strip()}")
        return "\n\n".join(parts)

    def list_cities(self) -> List[str]:
        if not KB_DIR.is_dir():
            return []
        return [p.stem.replace("旅行知识库", "") for p in sorted(KB_DIR.glob("*.md"))]


_kb: Optional[CityKnowledgeBase] = None


def get_knowledge_base() -> CityKnowledgeBase:
    global _kb
    if _kb is None:
        _kb = CityKnowledgeBase()
    return _kb


def retrieve_knowledge(city: str, query: str, top_k: int = 4) -> str:
    """便捷入口：检索城市知识，返回可直接注入提示词的文本。"""
    return get_knowledge_base().search(city, query, top_k=top_k)