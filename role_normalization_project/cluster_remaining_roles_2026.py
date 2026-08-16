"""对AI默认映射后仍未解决的招聘记录做第二轮语义聚类。

特征：岗位名称40% + JD职责30% + 能力画像25% + 搜索关键词5%。
使用互为Top-K近邻、功能族冲突约束和簇中心最低相似度，避免链式误合并。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

import numpy as np

from cli import DEFAULT_LOCAL_MODEL, extract_responsibilities
from concept_standardization.engine import parse_skills
from role_normalizer.embedding import SentenceTransformerEmbedder

PROJECT = Path(__file__).resolve().parent
REPOSITORY = PROJECT.parent
WORKSPACE = REPOSITORY.parent
DEFAULT_INPUT = WORKSPACE / "2026数据51job" / "jobs_2026_it_含能力提取结果_岗位归一化.csv"
DEFAULT_MAPPING = WORKSPACE / "2026数据51job" / "岗位概念标准化结果" / "job_role_mapping_draft.csv"
DEFAULT_OUTPUT = WORKSPACE / "2026数据51job" / "岗位概念标准化结果" / "second_round_clustering"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def primary_family(name: str) -> str:
    text = str(name or "").casefold()
    ordered = [
        ("test", r"测试|test|qa|验证"), ("sales", r"销售|售前|商务|市场推广"),
        ("product", r"产品经理|产品专员|product manager|需求分析"),
        ("operations", r"运维|devops|helpdesk|系统管理"),
        ("security", r"安全|渗透"), ("data", r"数据|bi|etl|数仓"),
        ("algorithm", r"算法|机器学习|深度学习|ai|人工智能|视觉|nlp|slam"),
        ("hardware", r"硬件|电子|电源|电气|芯片|ic|pcb|嵌入式"),
        ("development", r"开发|研发|程序|software|java|python|c\+\+|php|前端|后端"),
        ("project", r"项目经理|项目管理|pmo|项目专员"),
        ("content", r"短视频|剪辑|编导|新媒体|直播|影视|原画|动画"),
        ("admin", r"文员|行政|档案|资料员|秘书"),
        ("manufacturing", r"生产|工艺|设备|维修|操作工|装配|质量|iqc|技术员"),
        ("design", r"设计师|绘图|建模|模型师"),
        ("medical", r"临床|医药|医学|生物统计|生信"),
    ]
    for family, pattern in ordered:
        if re.search(pattern, text, re.IGNORECASE):
            return family
    return "unknown"


class UnionFind:
    def __init__(self, vectors: np.ndarray, families: list[str], max_size: int, min_centroid: float):
        self.parent = list(range(len(vectors))); self.members = {i: [i] for i in range(len(vectors))}
        self.vectors = vectors; self.families = {i: ({families[i]} - {"unknown"}) for i in range(len(vectors))}
        self.max_size = max_size; self.min_centroid = min_centroid

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]; x = self.parent[x]
        return x

    def union(self, left: int, right: int) -> bool:
        a, b = self.find(left), self.find(right)
        if a == b: return False
        members = self.members[a] + self.members[b]
        if len(members) > self.max_size: return False
        fa, fb = self.families[a], self.families[b]
        if fa and fb and fa != fb: return False
        centroid = self.vectors[members].mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm <= 1e-12: return False
        centroid /= norm
        if float((self.vectors[members] @ centroid).min()) < self.min_centroid: return False
        if len(self.members[a]) < len(self.members[b]): a, b = b, a
        self.parent[b] = a; self.members[a] = members; del self.members[b]
        self.families[a] = fa | fb; self.families.pop(b, None)
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="聚类AI默认映射后仍未解决的岗位记录")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=str(DEFAULT_LOCAL_MODEL))
    parser.add_argument("--device", default="")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--edge-threshold", type=float, default=0.84)
    parser.add_argument("--min-centroid-similarity", type=float, default=0.80)
    parser.add_argument("--max-cluster-size", type=int, default=50)
    args = parser.parse_args()

    mapping = {row["职位ID"]: row for row in read_csv(args.mapping)}
    rows = [row for row in read_csv(args.input)
            if mapping.get(str(row.get("职位ID") or ""), {}).get("匹配类型") == "PENDING"]
    print(f"待二次聚类记录：{len(rows):,}", flush=True)
    title_texts, responsibility_texts, skill_texts, search_texts, families = [], [], [], [], []
    for row in rows:
        candidate = str(row.get("岗位名称") or "").strip()
        original = str(row.get("原始职位名称") or "").strip()
        title_texts.append(f"{candidate} {original}".strip())
        responsibility_texts.append(extract_responsibilities(str(row.get("JD全文") or ""))[:1200])
        skill_texts.append(" ".join(parse_skills(str(row.get("能力提取结果") or ""))))
        search_texts.append(str(row.get("搜索关键词") or row.get("岗位关键词") or "").strip())
        families.append(primary_family(candidate + " " + original))

    embedder = SentenceTransformerEmbedder(args.model, device=args.device or None, batch_size=64)
    matrices = []
    for label, texts, weight in (("岗位名称", title_texts, 0.40), ("JD职责", responsibility_texts, 0.30),
                                 ("能力画像", skill_texts, 0.25), ("搜索关键词", search_texts, 0.05)):
        print(f"编码{label}...", flush=True)
        matrix = np.stack(embedder.encode(texts)).astype(np.float32, copy=False)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.maximum(norms, np.float32(1e-12))
        matrices.append(matrix * np.float32(math.sqrt(weight)))
    vectors = np.concatenate(matrices, axis=1)
    vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), np.float32(1e-12))

    count = len(rows); k = min(args.top_k, max(1, count - 1)); neighbors: list[set[int]] = [set() for _ in rows]
    scored: dict[tuple[int, int], float] = {}
    print(f"Top-{k}近邻召回...", flush=True)
    for start in range(0, count, 256):
        stop = min(count, start + 256); sims = vectors[start:stop] @ vectors.T
        for offset, values in enumerate(sims):
            source = start + offset; values[source] = -1
            indices = np.argpartition(values, -k)[-k:]
            for target in indices.tolist():
                neighbors[source].add(target)
                pair = (min(source, target), max(source, target))
                scored[pair] = max(scored.get(pair, -1), float(values[target]))
        print(f"\r近邻召回：{stop:,}/{count:,}", end="", flush=True)
    print(flush=True)

    edges = []
    generic_names = {"工程师", "技术员", "开发工程师", "项目经理", "项目专员", "项目助理", "系统工程师", "IT工程师", "应用工程师"}
    for (left, right), score in scored.items():
        mutual = right in neighbors[left] and left in neighbors[right]
        same_name = str(rows[left].get("岗位名称") or "").strip().casefold() == str(rows[right].get("岗位名称") or "").strip().casefold()
        non_generic_same_name = same_name and str(rows[left].get("岗位名称") or "").strip() not in generic_names
        if (score >= args.edge_threshold and mutual) or (non_generic_same_name and score >= 0.82):
            edges.append((score, left, right))
    edges.sort(reverse=True)
    print(f"候选连接：{len(edges):,}", flush=True)
    union = UnionFind(vectors, families, args.max_cluster_size, args.min_centroid_similarity)
    for _score, left, right in edges: union.union(left, right)
    groups = sorted(union.members.values(), key=lambda g: (-len(g), min(g)))

    cluster_rows, evidence_rows, singleton_rows = [], [], []
    for members in groups:
        if len(members) < 2:
            row = rows[members[0]]
            singleton_rows.append({"职位ID": row.get("职位ID", ""), "岗位名称": row.get("岗位名称", ""),
                                   "原始职位名称": row.get("原始职位名称", ""), "搜索关键词": row.get("搜索关键词", "")})
            continue
        ids = sorted(str(rows[i].get("职位ID") or "") for i in members)
        cluster_id = "cluster2:" + hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:16]
        names = Counter(str(rows[i].get("岗位名称") or "").strip() for i in members)
        originals = Counter(str(rows[i].get("原始职位名称") or "").strip() for i in members)
        skills = Counter(skill for i in members for skill in set(parse_skills(str(rows[i].get("能力提取结果") or ""))))
        searches = Counter(str(rows[i].get("搜索关键词") or rows[i].get("岗位关键词") or "").strip() for i in members)
        companies = {str(rows[i].get("公司全称") or "").strip() for i in members if str(rows[i].get("公司全称") or "").strip()}
        centroid = vectors[members].mean(axis=0); centroid /= max(float(np.linalg.norm(centroid)), 1e-12)
        cohesion = float((vectors[members] @ centroid).mean())
        representative = names.most_common(1)[0][0] or originals.most_common(1)[0][0]
        summary = {"cluster_id": cluster_id, "representative_name": representative, "record_count": len(members),
                   "company_count": len(companies), "name_count": len(names), "cohesion": round(cohesion, 4),
                   "top_names": "；".join(x for x, _ in names.most_common(8)),
                   "top_original_names": "；".join(x for x, _ in originals.most_common(8)),
                   "top_skills": "；".join(x for x, _ in skills.most_common(15)),
                   "search_keywords": "；".join(x for x, _ in searches.most_common(8))}
        cluster_rows.append(summary)
        evidence_rows.append({**summary, "record_ids": ids,
                              "sample_jds": [extract_responsibilities(str(rows[i].get("JD全文") or ""))[:400] for i in members[:3]]})

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fields = ["cluster_id", "representative_name", "record_count", "company_count", "name_count", "cohesion",
              "top_names", "top_original_names", "top_skills", "search_keywords"]
    write_csv(args.output_dir / "second_round_clusters.csv", cluster_rows, fields)
    write_csv(args.output_dir / "second_round_singletons.csv", singleton_rows,
              ["职位ID", "岗位名称", "原始职位名称", "搜索关键词"])
    with (args.output_dir / "second_round_cluster_evidence.jsonl").open("w", encoding="utf-8") as stream:
        for item in evidence_rows: stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    manifest = {"input_unresolved_records": count, "clusters": len(cluster_rows),
                "clustered_records": sum(int(x["record_count"]) for x in cluster_rows),
                "singletons": len(singleton_rows), "edge_threshold": args.edge_threshold,
                "min_centroid_similarity": args.min_centroid_similarity, "model": args.model}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
