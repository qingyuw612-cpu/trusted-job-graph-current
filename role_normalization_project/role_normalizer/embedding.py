"""岗位归一化使用的文本向量接口与实现。"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable


Vector = Sequence[float]


@runtime_checkable
class TextEmbedder(Protocol):
    """文本向量器协议，便于替换本地模型或测试实现。"""

    def encode(self, texts: Sequence[str]) -> list[Vector]:
        """批量编码文本，并返回与输入顺序一致的向量。"""


class HashingTextEmbedder:
    """无需第三方依赖的确定性哈希向量器。

    该实现适合单元测试和流程联调，不应代替生产语义模型。它同时提取
    规范化词项、中文字符 n-gram，并使用稳定哈希映射到固定维度。
    """

    _TOKEN_PATTERN = re.compile(r"[a-z0-9+#.]+|[\u4e00-\u9fff]+", re.IGNORECASE)

    def __init__(self, dimension: int = 384, char_ngram_range: tuple[int, int] = (2, 3)):
        """设置向量维度和中文字符 n-gram 范围。"""

        if dimension <= 0:
            raise ValueError("dimension 必须大于 0")
        minimum, maximum = char_ngram_range
        if minimum <= 0 or maximum < minimum:
            raise ValueError("char_ngram_range 必须是有效的正整数区间")
        self.dimension = dimension
        self.char_ngram_range = char_ngram_range

    @staticmethod
    def _normalize(text: str) -> str:
        """统一大小写和空白，保证同一文本产生稳定结果。"""

        return re.sub(r"\s+", " ", str(text or "").strip().casefold())

    def _features(self, text: str) -> list[str]:
        """提取词项和字符 n-gram 特征。"""

        features: list[str] = []
        for token in self._TOKEN_PATTERN.findall(self._normalize(text)):
            features.append(f"token:{token}")
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                minimum, maximum = self.char_ngram_range
                for size in range(minimum, maximum + 1):
                    features.extend(
                        f"char{size}:{token[index:index + size]}"
                        for index in range(max(0, len(token) - size + 1))
                    )
        return features

    def encode(self, texts: Sequence[str]) -> list[Vector]:
        """用特征哈希批量生成 L2 归一化向量。"""

        results: list[Vector] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
                index = int.from_bytes(digest[:8], "little") % self.dimension
                sign = 1.0 if digest[8] & 1 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
            results.append(vector)
        return results


class SentenceTransformerEmbedder:
    """延迟加载 sentence-transformers 的生产语义向量器。"""

    def __init__(
        self,
        model_name_or_path: str,
        *,
        device: str | None = None,
        batch_size: int = 64,
        normalize_embeddings: bool = True,
    ):
        """保存模型参数；直到第一次编码时才导入依赖并加载模型。"""

        if not model_name_or_path:
            raise ValueError("model_name_or_path 不能为空")
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")
        self.model_name_or_path = model_name_or_path
        self.device = device
        self.batch_size = batch_size
        self.normalize_embeddings = normalize_embeddings
        self._model = None

    def _load_model(self):
        """按需导入并创建 SentenceTransformer，避免基础流程强依赖模型包。"""

        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "使用 SentenceTransformerEmbedder 前请安装 sentence-transformers"
                ) from exc
            kwargs = {"device": self.device} if self.device else {}
            self._model = SentenceTransformer(self.model_name_or_path, **kwargs)
        return self._model

    def encode(self, texts: Sequence[str]) -> list[Vector]:
        """批量生成语义向量，并转换为普通 Python 列表。"""

        values = [str(text or "") for text in texts]
        if not values:
            return []
        embeddings = self._load_model().encode(
            values,
            batch_size=self.batch_size,
            normalize_embeddings=self.normalize_embeddings,
            show_progress_bar=False,
        )
        # 保留模型返回的 float32 数组视图，避免大批量任务转换成 Python
        # float 列表后产生数倍内存开销。评分器只依赖 Sequence 接口。
        return [vector for vector in embeddings]
