"""未映射岗位之间的混合相似度与可扩展阻塞键。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Sequence

from .embedding import TextEmbedder, Vector
from .models import JobTitleRecord
from .scoring import build_match_scores, cosine_similarity, lexical_similarity


class RecordPairSimilarity:
    """计算两条岗位记录的名称、职责、技能和字面混合相似度。"""

    def __init__(self, embedder: TextEmbedder, weights: Mapping[str, float]):
        """注入向量器和评分权重，并建立文本向量缓存。"""

        self.embedder = embedder
        self.weights = dict(weights)
        self._cache: dict[str, Vector] = {}

    def _vector(self, text: str) -> Vector:
        """缓存相同文本的向量，避免候选对评分时重复编码。"""

        value = str(text or "").strip()
        if not value:
            return []
        if value not in self._cache:
            self._cache[value] = self.embedder.encode([value])[0]
        return self._cache[value]

    def seed_record_vectors(
        self,
        record: JobTitleRecord,
        vectors: tuple[Vector, Vector, Vector],
    ) -> None:
        """注入解析阶段已生成的三类向量，避免为同一记录重复编码。"""

        values = (
            record.normalized_name.strip(),
            record.responsibilities.strip(),
            self._skills(record),
        )
        for text, vector in zip(values, vectors):
            if text:
                self._cache[text] = vector

    def prepare(self, records: list[JobTitleRecord], batch_size: int = 512) -> None:
        """批量预计算候选聚类所需文本向量，避免逐候选对调用模型。"""

        texts: set[str] = set()
        for record in records:
            texts.update(
                value
                for value in (
                    record.normalized_name.strip(),
                    record.responsibilities.strip(),
                    self._skills(record),
                )
                if value and value not in self._cache
            )
        ordered = sorted(texts)
        for start in range(0, len(ordered), batch_size):
            batch = ordered[start : start + batch_size]
            vectors = self.embedder.encode(batch)
            self._cache.update(zip(batch, vectors))

    def candidate_pairs(
        self,
        records: Sequence[JobTitleRecord],
        *,
        top_k: int = 12,
        chunk_size: int = 256,
    ) -> list[tuple[int, int]]:
        """用名称向量 Top-K 召回候选对，避免阻塞块内数百万次全量互比。"""

        if top_k < 1 or chunk_size < 1:
            raise ValueError("top_k 和 chunk_size 必须大于等于 1")
        if len(records) < 2:
            return []
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("向量 Top-K 候选召回需要 numpy") from exc

        indexed_vectors: list[tuple[int, Vector]] = []
        expected_dimension: int | None = None
        for index, record in enumerate(records):
            vector = self._vector(record.normalized_name)
            # 清洗结果为空的记录没有可靠的名称语义，保留为 UNMAPPED，
            # 但不参与名称向量近邻聚类。
            if len(vector) == 0:
                continue
            if expected_dimension is None:
                expected_dimension = len(vector)
            elif len(vector) != expected_dimension:
                raise ValueError(
                    "名称向量维度不一致："
                    f"期望 {expected_dimension}，记录 {index} 实际为 {len(vector)}"
                )
            indexed_vectors.append((index, vector))
        if len(indexed_vectors) < 2:
            return []

        original_indices = [index for index, _vector_value in indexed_vectors]
        matrix = np.stack(
            [vector for _index, vector in indexed_vectors]
        ).astype(np.float32, copy=False)
        # 兼容未做 L2 归一化的可替换向量器。
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.maximum(norms, np.float32(1e-12))
        neighbor_count = min(top_k, len(indexed_vectors) - 1)
        pairs: set[tuple[int, int]] = set()
        for start in range(0, len(indexed_vectors), chunk_size):
            stop = min(start + chunk_size, len(indexed_vectors))
            similarities = matrix[start:stop] @ matrix.T
            for offset, row in enumerate(similarities):
                source_local = start + offset
                row[source_local] = -1.0
                indices = np.argpartition(row, -neighbor_count)[-neighbor_count:]
                source = original_indices[source_local]
                for target_local in indices.tolist():
                    target = original_indices[target_local]
                    pairs.add((min(source, target), max(source, target)))
        return sorted(pairs)

    @staticmethod
    def _skills(record: JobTitleRecord) -> str:
        """把技能集合转换为稳定文本。"""

        return " ".join(sorted({str(item).strip() for item in record.skills if str(item).strip()}))

    def __call__(self, left: JobTitleRecord, right: JobTitleRecord) -> float:
        """返回 0～1 的动态权重混合相似度。"""

        left_skills, right_skills = self._skills(left), self._skills(right)
        effective = dict(self.weights)
        if not left.responsibilities or not right.responsibilities:
            effective["responsibility"] = 0.0
        if not left_skills or not right_skills:
            effective["skill"] = 0.0
        scores = build_match_scores(
            title_similarity=cosine_similarity(
                self._vector(left.normalized_name), self._vector(right.normalized_name)
            ),
            responsibility_similarity=cosine_similarity(
                self._vector(left.responsibilities), self._vector(right.responsibilities)
            ),
            skill_similarity=cosine_similarity(
                self._vector(left_skills), self._vector(right_skills)
            ),
            lexical_similarity_value=lexical_similarity(
                left.normalized_name, right.normalized_name
            ),
            weights=effective,
        )
        return scores.combined_similarity


def default_blocking_keys(record: JobTitleRecord) -> set[str]:
    """按精确名称及“搜索类别＋有效名称片段”分块，避免组内全量互比。"""

    normalized = re.sub(r"\s+", "", record.normalized_name.casefold())
    search = record.search_name.casefold().strip() or "unknown"
    keys = {f"name:{normalized}"} if normalized else set()
    generic = {
        "工程", "程师", "工程师", "经理", "主管", "专员", "助理", "岗位",
        "开发工程师", "技术", "人员", "员工", "高级", "中级", "初级",
    }
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", normalized)
    fragments: set[str] = set()
    for run in chinese_runs:
        for size in (2, 3):
            fragments.update(
                run[index : index + size]
                for index in range(max(0, len(run) - size + 1))
            )
    fragments.difference_update(generic)
    ascii_tokens = re.findall(r"[a-z][a-z0-9+#.]{1,}", normalized)
    fragments.update(ascii_tokens)
    for fragment in sorted(fragments)[:16]:
        keys.add(f"search:{search}|fragment:{fragment}")
    if not fragments:
        fallback = normalized or record.original_name.casefold()
        keys.add(f"fallback:{search}:{fallback[:2]}:{fallback[-3:]}")
    return keys
