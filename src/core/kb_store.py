"""知识库：加载 kb/*.md，提供 retrieve(query, top_k)。

优先使用 Chroma（若已安装且 USE_CHROMA=1 启用），否则退化为本地关键词检索，
保证 W1 无第三方依赖也能演示 RAG 思路。
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import KB_DIR

USE_CHROMA = os.getenv("USE_CHROMA", "0") == "1"
try:
    if USE_CHROMA:
        import chromadb
        _HAS_CHROMA = True
    else:
        _HAS_CHROMA = False
except ImportError:
    _HAS_CHROMA = False


def _load_docs():
    docs = []
    for md in Path(KB_DIR).glob("*.md"):
        text = md.read_text(encoding="utf-8")
        for chunk in text.split("\n\n"):
            chunk = chunk.strip()
            if len(chunk) > 20:
                docs.append({"source": md.name, "text": chunk})
    return docs


class KBStore:
    def __init__(self):
        self.docs = _load_docs()
        if _HAS_CHROMA:
            self._client = chromadb.PersistentClient(path=str(ROOT / ".chroma"))
            self._col = self._client.get_or_create_collection("teleops_kb")
            if self._col.count() == 0 and self.docs:
                self._col.add(
                    ids=[str(i) for i in range(len(self.docs))],
                    documents=[d["text"] for d in self.docs],
                    metadatas=[{"source": d["source"]} for d in self.docs],
                )

    def retrieve(self, query: str, top_k: int = 3):
        if _HAS_CHROMA:
            res = self._col.query(query_texts=[query], n_results=top_k)
            return [{"source": m["source"], "text": d}
                    for d, m in zip(res["documents"][0], res["metadatas"][0])]
        # 兜底：字符级重叠打分（中文无空格，按字匹配）
        q_chars = set(query) - set(" \n\t，。、：；！？（）.,:;!?()")
        scored = []
        for d in self.docs:
            d_chars = set(d["text"])
            score = len(q_chars & d_chars)
            if score > 0:
                scored.append((score, d))
        scored.sort(key=lambda x: -x[0])
        return [{"source": d["source"], "text": d["text"]} for _, d in scored[:top_k]]


if __name__ == "__main__":
    kb = KBStore()
    print("知识库片段数:", len(kb.docs))
    for r in kb.retrieve("核心交换机 端口 拥塞"):
        print("-", r["source"], ":", r["text"][:60])
