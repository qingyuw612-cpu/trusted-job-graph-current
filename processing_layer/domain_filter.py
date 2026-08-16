from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402
from trusted_graph_agent.text_utils import normalize_text  # noqa: E402


DEFAULT_POLICY = PROJECT_ROOT / "config" / "it_domain_filter.json"
DEFAULT_MODEL = (
    PROJECT_ROOT
    / "models"
    / "hf_cache"
    / "hub"
    / "models--BAAI--bge-small-zh-v1.5"
    / "snapshots"
    / "7999e1d3359715c523056ef9478215996d62a620"
)


class TextEmbedder(Protocol):
    def encode(self, texts: list[str]) -> Any: ...


@dataclass(slots=True)
class DomainDecision:
    label: str
    score: float
    confidence: float
    structured_score: float
    semantic_score: float | None
    positive_similarity: float | None
    negative_similarity: float | None
    positive_groups: list[str]
    negative_groups: list[str]
    reasons: list[str]


@dataclass(slots=True)
class StructuredAssessment:
    score: float
    positive_groups: list[str]
    negative_groups: list[str]
    reasons: list[str]
    hard_label: str = ""


@dataclass(slots=True)
class FilterMetrics:
    rows_read: int = 0
    it: int = 0
    non_it: int = 0
    uncertain: int = 0
    semantic_evaluated: int = 0
    batches_written: int = 0
    reason_counts: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"IT": [], "NON_IT": [], "UNCERTAIN": []}
    )
    errors: list[str] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def contains_any(text: str, terms: list[str]) -> list[str]:
    lowered = text.casefold()
    return [term for term in terms if term.casefold() in lowered]


class StructuredITScorer:
    def __init__(self, policy: dict[str, Any], taxonomy_path: Path):
        self.policy = policy
        taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
        self.role_metadata: dict[str, dict[str, Any]] = {}
        self.all_role_aliases: list[str] = []
        for role in taxonomy["roles"]:
            values = [role["role_name"], *role.get("aliases", [])]
            metadata = {
                "role_name": role["role_name"],
                "family_id": role["family_id"],
                "aliases": values,
            }
            for value in values:
                normalized = normalize_text(value)
                self.role_metadata[normalized] = metadata
                if len(normalized) >= 4:
                    self.all_role_aliases.append(normalized)
        self.strong_families = set(policy["strong_it_families"])

    def role(self, declared_role: str) -> dict[str, Any]:
        return self.role_metadata.get(normalize_text(declared_role), {})

    def assess(self, row: dict[str, Any]) -> StructuredAssessment:
        title = str(row.get("title") or "").strip()
        declared_role = str(row.get("declared_role") or "").strip()
        industry = str(row.get("industry") or "").strip()
        description = str(row.get("description") or "")
        tags = str(row.get("tags") or "")
        evidence_text = "；".join((title, tags, description[:3000]))
        role = self.role(declared_role)
        family = str(role.get("family_id") or "")
        aliases = role.get("aliases") or [declared_role]
        normalized_title = normalize_text(title)
        title_matches_role = any(
            normalize_text(alias)
            and (
                normalize_text(alias) in normalized_title
                or normalized_title in normalize_text(alias)
            )
            for alias in aliases
        )
        title_matches_it_taxonomy = any(
            alias in normalized_title or normalized_title in alias
            for alias in self.all_role_aliases
            if normalized_title
        )

        positive_groups = [
            name
            for name, terms in self.policy["positive_signal_groups"].items()
            if contains_any(evidence_text, terms)
        ]
        negative_groups = [
            name
            for name, terms in self.policy["negative_signal_groups"].items()
            if contains_any(evidence_text, terms)
        ]
        technical_title_hits = contains_any(title, self.policy["technical_title_terms"])
        non_it_title_hits = contains_any(title, self.policy["non_it_title_terms"])
        positive_industry_hits = contains_any(industry, self.policy["positive_industries"])
        negative_industry_hits = contains_any(industry, self.policy["negative_industries"])

        score = 0.38
        reasons: list[str] = []
        if family:
            role_prior = 0.17 if family in self.strong_families else 0.08
            score += role_prior
            reasons.append(f"taxonomy_family:{family}")
        else:
            score -= 0.12
            reasons.append("taxonomy_role_missing")
        if title_matches_role:
            score += 0.16 if family in self.strong_families else 0.10
            reasons.append("title_matches_taxonomy_role")
        elif title_matches_it_taxonomy:
            score += 0.08
            reasons.append("title_matches_it_taxonomy")
        if technical_title_hits:
            score += 0.20
            reasons.append("technical_title:" + technical_title_hits[0])
        score += min(len(positive_groups), 3) * 0.105
        if positive_groups:
            reasons.append("positive_groups:" + ",".join(positive_groups))
        if positive_industry_hits:
            score += 0.08
            reasons.append("positive_industry:" + positive_industry_hits[0])

        score -= min(len(negative_groups), 2) * 0.13
        if negative_groups:
            reasons.append("negative_groups:" + ",".join(negative_groups))
        if negative_industry_hits:
            score -= 0.16
            reasons.append("negative_industry:" + negative_industry_hits[0])
        if non_it_title_hits and not technical_title_hits:
            score -= 0.32
            reasons.append("non_it_title:" + non_it_title_hits[0])

        score = max(0.0, min(1.0, score))
        hard_label = ""
        if (
            non_it_title_hits
            and not technical_title_hits
            and len(positive_groups) <= 1
        ):
            hard_label = "NON_IT"
        elif (
            negative_industry_hits
            and not technical_title_hits
            and not positive_groups
            and family not in self.strong_families
        ):
            hard_label = "NON_IT"
        elif (
            family in self.strong_families
            and title_matches_role
            and not non_it_title_hits
        ):
            hard_label = "IT"
        elif (
            technical_title_hits
            and (positive_groups or positive_industry_hits)
            and not non_it_title_hits
        ):
            hard_label = "IT"
        elif (
            title_matches_it_taxonomy
            and positive_industry_hits
            and not negative_industry_hits
            and not non_it_title_hits
        ):
            hard_label = "IT"
        elif (
            len(positive_groups) >= 2
            and positive_industry_hits
            and not non_it_title_hits
        ):
            hard_label = "IT"
        elif len(positive_groups) >= 2 and score >= 0.68 and not non_it_title_hits:
            hard_label = "IT"
        return StructuredAssessment(
            score=score,
            positive_groups=positive_groups,
            negative_groups=negative_groups,
            reasons=reasons,
            hard_label=hard_label,
        )


class SentenceTransformerDomainEmbedder:
    def __init__(self, model_path: Path, batch_size: int, device: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(
                "缺少 sentence-transformers，无法执行语义领域判定。"
            ) from error
        self.model = SentenceTransformer(str(model_path), device=device)
        self.batch_size = max(1, batch_size)

    def encode(self, texts: list[str]):
        return self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )


class HybridITDomainClassifier:
    def __init__(
        self,
        policy_path: Path = DEFAULT_POLICY,
        taxonomy_path: Path | None = None,
        embedder: TextEmbedder | None = None,
    ):
        self.policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.version = str(self.policy["version"])
        self.thresholds = self.policy["thresholds"]
        self.structured = StructuredITScorer(
            self.policy,
            taxonomy_path
            or PROJECT_ROOT / "trusted_graph_agent" / "it_role_taxonomy.json",
        )
        self.embedder = embedder
        self.positive_vectors = None
        self.negative_vectors = None
        self.semantic_cache: dict[str, tuple[float, float, float]] = {}
        if embedder is not None:
            self.positive_vectors = embedder.encode(self.policy["positive_prototypes"])
            self.negative_vectors = embedder.encode(self.policy["negative_prototypes"])

    @staticmethod
    def semantic_key(
        row: dict[str, Any],
        assessment: StructuredAssessment,
    ) -> str:
        return "|".join(
            (
                normalize_text(str(row.get("title") or "")),
                normalize_text(str(row.get("declared_role") or "")),
                normalize_text(str(row.get("industry") or "")),
                ",".join(assessment.positive_groups),
                ",".join(assessment.negative_groups),
            )
        )

    @staticmethod
    def semantic_text(
        row: dict[str, Any],
        assessment: StructuredAssessment,
    ) -> str:
        return (
            f"实际岗位：{row.get('title') or ''}；"
            f"来源角色：{row.get('declared_role') or ''}；"
            f"行业：{row.get('industry') or ''}；"
            f"职责技术证据：{','.join(assessment.positive_groups) or '无'}；"
            f"职责非技术证据：{','.join(assessment.negative_groups) or '无'}"
        )

    def classify_batch(self, rows: list[dict[str, Any]]) -> list[DomainDecision]:
        import numpy as np

        assessments = [self.structured.assess(row) for row in rows]
        decisions: list[DomainDecision | None] = [None] * len(rows)
        ambiguous_indices: list[int] = []
        for index, assessment in enumerate(assessments):
            if assessment.hard_label:
                decisions[index] = self._decision(
                    assessment,
                    assessment.hard_label,
                    assessment.score,
                    semantic_score=None,
                    positive_similarity=None,
                    negative_similarity=None,
                )
            else:
                ambiguous_indices.append(index)

        if ambiguous_indices and self.embedder is not None:
            positive_vectors = np.asarray(self.positive_vectors, dtype="float32")
            negative_vectors = np.asarray(self.negative_vectors, dtype="float32")
            missing_keys: list[str] = []
            missing_texts: list[str] = []
            for index in ambiguous_indices:
                key = self.semantic_key(rows[index], assessments[index])
                if key not in self.semantic_cache and key not in missing_keys:
                    missing_keys.append(key)
                    missing_texts.append(
                        self.semantic_text(rows[index], assessments[index])
                    )
            if missing_texts:
                vectors = np.asarray(
                    self.embedder.encode(missing_texts),
                    dtype="float32",
                )
                for key, vector in zip(missing_keys, vectors):
                    positive_similarity = float(np.max(positive_vectors @ vector))
                    negative_similarity = float(np.max(negative_vectors @ vector))
                    semantic_score = max(
                        0.0,
                        min(
                            1.0,
                            0.5
                            + (positive_similarity - negative_similarity) * 2.6,
                        ),
                    )
                    self.semantic_cache[key] = (
                        semantic_score,
                        positive_similarity,
                        negative_similarity,
                    )
            for index in ambiguous_indices:
                key = self.semantic_key(rows[index], assessments[index])
                (
                    semantic_score,
                    positive_similarity,
                    negative_similarity,
                ) = self.semantic_cache[key]
                weight = float(self.thresholds["semantic_weight"])
                final_score = (
                    assessments[index].score * (1.0 - weight)
                    + semantic_score * weight
                )
                label = self._label(final_score, assessments[index])
                decisions[index] = self._decision(
                    assessments[index],
                    label,
                    final_score,
                    semantic_score,
                    positive_similarity,
                    negative_similarity,
                )
        else:
            for index in ambiguous_indices:
                score = assessments[index].score
                label = self._label(score, assessments[index])
                decisions[index] = self._decision(
                    assessments[index],
                    label,
                    score,
                    semantic_score=None,
                    positive_similarity=None,
                    negative_similarity=None,
                )
        return [decision for decision in decisions if decision is not None]

    def _label(
        self,
        score: float,
        assessment: StructuredAssessment,
    ) -> str:
        accept = float(self.thresholds["accept"])
        reject = float(self.thresholds["reject"])
        minimum_groups = int(self.thresholds["minimum_positive_groups"])
        if score >= accept and len(assessment.positive_groups) >= minimum_groups:
            return "IT"
        if score <= reject:
            return "NON_IT"
        return "UNCERTAIN"

    @staticmethod
    def _decision(
        assessment: StructuredAssessment,
        label: str,
        score: float,
        semantic_score: float | None,
        positive_similarity: float | None,
        negative_similarity: float | None,
    ) -> DomainDecision:
        confidence = (
            abs(score - 0.5) * 2
            if label != "UNCERTAIN"
            else max(0.0, 1.0 - abs(score - 0.52) * 2)
        )
        return DomainDecision(
            label=label,
            score=round(score, 6),
            confidence=round(max(0.0, min(1.0, confidence)), 6),
            structured_score=round(assessment.score, 6),
            semantic_score=round(semantic_score, 6)
            if semantic_score is not None
            else None,
            positive_similarity=round(positive_similarity, 6)
            if positive_similarity is not None
            else None,
            negative_similarity=round(negative_similarity, 6)
            if negative_similarity is not None
            else None,
            positive_groups=assessment.positive_groups,
            negative_groups=assessment.negative_groups,
            reasons=assessment.reasons,
        )


FETCH_QUERY = """
MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
WHERE coalesce(raw.domain_classifier_version, '') <> $classifier_version
RETURN properties(raw) AS raw
LIMIT $batch_size
"""


WRITE_QUERY = """
UNWIND $rows AS row
MATCH (raw:RawJDVersion {version_id:row.version_id})
SET raw.domain_label=row.domain_label,
    raw.domain_score=row.domain_score,
    raw.domain_confidence=row.domain_confidence,
    raw.domain_structured_score=row.domain_structured_score,
    raw.domain_semantic_score=row.domain_semantic_score,
    raw.domain_positive_similarity=row.domain_positive_similarity,
    raw.domain_negative_similarity=row.domain_negative_similarity,
    raw.domain_positive_groups=row.domain_positive_groups,
    raw.domain_negative_groups=row.domain_negative_groups,
    raw.domain_reasons=row.domain_reasons,
    raw.domain_classifier_version=$classifier_version,
    raw.domain_classified_at=$now
WITH raw, row
OPTIONAL MATCH (raw)-[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD)
SET processed.domain_label=row.domain_label,
    processed.domain_score=row.domain_score,
    processed.domain_classifier_version=$classifier_version
RETURN count(raw) AS classified
"""


class Neo4jDomainFilter:
    def __init__(
        self,
        repository: Neo4jGraphRepository,
        classifier: HybridITDomainClassifier,
        batch_size: int,
    ):
        self.client = repository.client
        self.classifier = classifier
        self.batch_size = max(1, min(batch_size, 1000))

    def query_with_retry(
        self,
        statement: str,
        parameters: dict[str, Any] | None = None,
        *,
        write: bool = False,
        attempts: int = 8,
    ) -> list[dict[str, Any]]:
        for attempt in range(1, attempts + 1):
            try:
                return self.client.query(
                    statement,
                    parameters,
                    access_mode="Write" if write else "Read",
                )
            except (ValueError, TimeoutError, OSError) as error:
                if attempt == attempts:
                    raise
                delay = min(2**attempt, 30)
                print(
                    f"neo4j_retry={attempt}/{attempts - 1} waiting={delay}s "
                    f"error={str(error)[:180]}",
                    flush=True,
                )
                time.sleep(delay)
        raise RuntimeError("Neo4j 重试状态异常")

    def run(self, limit: int = 0, dry_run: bool = False) -> FilterMetrics:
        metrics = FilterMetrics()
        reasons: Counter[str] = Counter()
        while not limit or metrics.rows_read < limit:
            page_size = (
                min(self.batch_size, limit - metrics.rows_read)
                if limit
                else self.batch_size
            )
            rows = self.query_with_retry(
                FETCH_QUERY,
                {
                    "classifier_version": self.classifier.version,
                    "batch_size": page_size,
                },
            )
            if not rows:
                break
            raw_rows = [dict(row.get("raw") or {}) for row in rows]
            decisions = self.classifier.classify_batch(raw_rows)
            output_rows: list[dict[str, Any]] = []
            for raw, decision in zip(raw_rows, decisions):
                metrics.rows_read += 1
                if decision.label == "IT":
                    metrics.it += 1
                elif decision.label == "NON_IT":
                    metrics.non_it += 1
                else:
                    metrics.uncertain += 1
                if decision.semantic_score is not None:
                    metrics.semantic_evaluated += 1
                reasons.update(decision.reasons)
                if len(metrics.samples[decision.label]) < 12:
                    metrics.samples[decision.label].append(
                        {
                            "version_id": str(raw.get("version_id") or ""),
                            "title": str(raw.get("title") or ""),
                            "declared_role": str(raw.get("declared_role") or ""),
                            "industry": str(raw.get("industry") or ""),
                            "score": decision.score,
                            "positive_groups": decision.positive_groups,
                            "negative_groups": decision.negative_groups,
                        }
                    )
                output_rows.append(
                    {
                        "version_id": str(raw.get("version_id") or ""),
                        "domain_label": decision.label,
                        "domain_score": decision.score,
                        "domain_confidence": decision.confidence,
                        "domain_structured_score": decision.structured_score,
                        "domain_semantic_score": decision.semantic_score,
                        "domain_positive_similarity": decision.positive_similarity,
                        "domain_negative_similarity": decision.negative_similarity,
                        "domain_positive_groups": decision.positive_groups,
                        "domain_negative_groups": decision.negative_groups,
                        "domain_reasons": decision.reasons,
                    }
                )
            if dry_run:
                break
            self.query_with_retry(
                WRITE_QUERY,
                {
                    "rows": output_rows,
                    "classifier_version": self.classifier.version,
                    "now": utc_now(),
                },
                write=True,
            )
            metrics.batches_written += 1
            print(
                f"classified={metrics.rows_read} it={metrics.it} "
                f"non_it={metrics.non_it} uncertain={metrics.uncertain} "
                f"semantic={metrics.semantic_evaluated}",
                flush=True,
            )
        metrics.reason_counts = dict(reasons.most_common(30))
        return metrics


def graph_status(repository: Neo4jGraphRepository, version: str) -> dict[str, Any]:
    rows = repository.client.query(
        """
        CALL {
          MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
          RETURN count(raw) AS current_total
        }
        CALL {
          MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
          WHERE raw.domain_classifier_version=$version
          RETURN count(raw) AS classified,
                 count(CASE WHEN raw.domain_label='IT' THEN 1 END) AS it,
                 count(CASE WHEN raw.domain_label='NON_IT' THEN 1 END) AS non_it,
                 count(CASE WHEN raw.domain_label='UNCERTAIN' THEN 1 END) AS uncertain
        }
        RETURN current_total, classified, it, non_it, uncertain
        """,
        {"version": version},
    )
    return rows[0] if rows else {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="使用岗位分类表、职责证据、行业证据和本地语义向量筛选信息技术JD"
    )
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--embedding-batch-size", type=int, default=64)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-semantic", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="可选 JSON 报告路径；不传时只输出到终端。",
    )
    args = parser.parse_args()

    policy_path = args.policy.resolve()
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    repository = Neo4jGraphRepository(args.neo4j_config.resolve())
    if args.status_only:
        print(
            json.dumps(
                graph_status(repository, str(policy["version"])),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    preflight = graph_status(repository, str(policy["version"]))
    if (
        not args.dry_run
        and int(preflight.get("classified") or 0)
        >= int(preflight.get("current_total") or 0)
    ):
        print(
            json.dumps(
                {
                    "classifier_version": str(policy["version"]),
                    "status": "ALREADY_COMPLETE",
                    "graph_status": preflight,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    embedder = None
    if not args.no_semantic:
        model_path = args.model.resolve()
        if not model_path.exists():
            raise FileNotFoundError(f"本地语义模型不存在：{model_path}")
        embedder = SentenceTransformerDomainEmbedder(
            model_path,
            args.embedding_batch_size,
            args.device,
        )
    classifier = HybridITDomainClassifier(policy_path, embedder=embedder)
    started_at = utc_now()
    metrics = Neo4jDomainFilter(
        repository,
        classifier,
        args.batch_size,
    ).run(args.limit, args.dry_run)
    payload = {
        "classifier_version": classifier.version,
        "policy_name": classifier.policy["policy_name"],
        "started_at": started_at,
        "finished_at": utc_now(),
        "dry_run": args.dry_run,
        "semantic_enabled": embedder is not None,
        "metrics": asdict(metrics),
        "graph_status": graph_status(repository, classifier.version)
        if not args.dry_run
        else {},
    }
    if not args.dry_run and args.report:
        args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.report.resolve().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
