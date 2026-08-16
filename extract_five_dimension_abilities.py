"""从原始 JD 批量提取五维能力，并写入“能力提取结果”字段。

支持 CSV、JSON、JSONL；使用 OpenAI 兼容的 ``/chat/completions`` 接口。
输出与现有 AbilityAnalysisExtractor 完全兼容：知识、技术、动机、特质、自我概念。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from raw_jd_layer.importer import FIELD_ALIASES, iter_records, normalize_key, stringify


DIMENSIONS = ("知识", "技术", "动机", "特质", "自我概念")
IFLYTEK_SPARK_BASE_URL = "https://spark-api-open.xf-yun.com/v1"
OUTPUT_FIELD = "能力提取结果"
EMPTY_VALUES = {"", "无", "未提及", "未明确", "无相关内容", "none", "null"}
LIST_SPLIT = re.compile(r"[、，,；;\n]+")
SPACE = re.compile(r"\s+")
META_NOISE = re.compile(
    r"(?:未提及|未明确|无相关|没有提到|无法判断|根据(?:该|此)?(?:岗位|JD)|"
    r"原文(?:中)?|能力要素|以上内容|候选人需要|应聘者需要)",
    re.IGNORECASE,
)
CONDITION_NOISE = re.compile(
    r"(?:学历|本科|硕士|博士|大专|专业(?:优先|要求|背景|限制|$)|工作经验|项目经验|行业经验|开发经验|实习经验|"
    r"\d+\s*年(?:以上|以下)?经验|薪资|工资|工作地点|年龄|性别|招聘人数|五险|一金|福利)",
    re.IGNORECASE,
)
SYSTEM_PROMPT = """你是招聘岗位能力分析器。只从提供的 JD 原文中提取明确出现的能力短语。

使用五维能力模型：
1. 知识：岗位要求理解或掌握的理论、领域、业务、规范和原理。
2. 技术：可操作的工具、语言、框架、方法、流程和专业技能。
3. 动机：原文明确表达的主动性、学习、创新、结果或目标驱动力。
4. 特质：原文明确要求的沟通、协作、逻辑、抗压、严谨等稳定特征。
5. 自我概念：原文明确要求的责任心、客户导向、职业认同、领导担当等自我角色认知。

强制规则：
- 每项必须是 JD 原文中可逐字或忽略空白/标点后回查到的短语，不得推断、扩写或补充常识。
- 不提取学历、专业、工作年限、薪资、地点、年龄、性别、招聘人数和福利。
- 不输出“未提及”“无明确要求”等解释性文字；没有证据的维度返回空数组。
- 短语应简洁，保留技术专名和关键限定，去重；每个维度最多 12 项。
- 只返回 JSON 对象，不要 Markdown，不要解释。
"""


def read_environment(name: str) -> str:
    """Read a secret/config value without persisting it in project files."""
    value = os.getenv(name, "").strip()
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            stored, _ = winreg.QueryValueEx(key, name)
        return str(stored or "").strip()
    except (ImportError, FileNotFoundError, OSError):
        return ""


def alias_values(field: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(normalize_key(alias) for alias in FIELD_ALIASES[field]))


TITLE_KEYS = alias_values("title")
DESCRIPTION_KEYS = alias_values("description")
TAGS_KEYS = alias_values("tags")
ABILITY_KEYS = alias_values("ability_analysis")


def pick_alias(record: dict[str, Any], keys: Iterable[str]) -> str:
    normalized = {normalize_key(key): value for key, value in record.items()}
    for key in keys:
        value = stringify(normalized.get(key))
        if value and value.lower() not in {"nan", "none", "null"}:
            return value
    return ""


def normalize_for_evidence(value: str) -> str:
    return "".join(
        character.lower()
        for character in value
        if re.fullmatch(r"[0-9a-z\u4e00-\u9fff+#.]", character.lower())
    )


def evidence_present(term: str, source: str) -> bool:
    needle = normalize_for_evidence(term)
    return len(needle) >= 2 and needle in normalize_for_evidence(source)


def clean_term(value: Any) -> str:
    text = SPACE.sub(" ", str(value or "")).strip(" \t\r\n。；;，,：:\"'[]")
    text = re.sub(r"^(?:[-—•·]|\d+\s*[.、．])\s*", "", text).strip()
    return text[:120]


def string_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [clean_term(item) for item in value]
    if isinstance(value, dict):
        return [clean_term(item) for item in value.values()]
    if isinstance(value, str):
        return [clean_term(item) for item in LIST_SPLIT.split(value)]
    return []


def validate_result(payload: Any, evidence_text: str) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("模型输出不是 JSON 对象。")
    result: dict[str, list[str]] = {dimension: [] for dimension in DIMENSIONS}
    for dimension in DIMENSIONS:
        seen: set[str] = set()
        for term in string_items(payload.get(dimension)):
            normalized = normalize_for_evidence(term)
            if (
                not term
                or term.lower() in EMPTY_VALUES
                or META_NOISE.search(term)
                or CONDITION_NOISE.search(term)
                or normalized in seen
                or not evidence_present(term, evidence_text)
            ):
                continue
            seen.add(normalized)
            result[dimension].append(term)
            if len(result[dimension]) >= 12:
                break
    return result


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型响应中没有 JSON 对象。")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型响应 JSON 不是对象。")
    return value


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int,
        retries: int,
        response_mode: str = "json-schema",
    ) -> None:
        url = base_url.strip().rstrip("/")
        if url and not re.match(r"^https?://", url, flags=re.IGNORECASE):
            url = "https://" + url
        self.url = url if url.endswith("/chat/completions") else url + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = max(5, timeout)
        self.retries = max(0, retries)
        self.response_mode = response_mode

    def extract(self, title: str, description: str, tags: str) -> tuple[dict[str, Any], dict[str, int]]:
        user_prompt = (
            f"岗位名称：{title or '未提供'}\n"
            f"职位标签：{tags or '未提供'}\n"
            f"JD原文：\n{description}"
        )
        schema = {
            "type": "object",
            "properties": {
                dimension: {"type": "array", "items": {"type": "string"}}
                for dimension in DIMENSIONS
            },
            "required": list(DIMENSIONS),
            "additionalProperties": False,
        }
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": (
                [{"role": "user", "content": f"{SYSTEM_PROMPT}\n\n{user_prompt}"}]
                if self.model.strip().lower() == "lite"
                else [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ]
            ),
        }
        if self.response_mode == "json-schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "five_dimension_job_abilities",
                    "strict": True,
                    "schema": schema,
                },
            }
        elif self.response_mode == "json-object":
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(self.url, data=data, headers=headers, method="POST")
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                choice = result["choices"][0]
                content = choice["message"].get("content")
                if isinstance(content, list):
                    content = "".join(
                        str(item.get("text") or "") if isinstance(item, dict) else str(item)
                        for item in content
                    )
                usage = result.get("usage") or {}
                return extract_json_object(str(content or "")), {
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                }
            except (
                HTTPError,
                URLError,
                HTTPException,
                ConnectionError,
                TimeoutError,
                KeyError,
                IndexError,
                json.JSONDecodeError,
                ValueError,
            ) as error:
                last_error = error
                if attempt >= self.retries:
                    break
                time.sleep(min(20.0, (2**attempt) + random.random()))
        raise RuntimeError(f"大模型请求失败：{last_error}")


class JsonlCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.values: dict[str, dict[str, Any]] = {}
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("key") and isinstance(item.get("result"), dict):
                        self.values[str(item["key"])] = item["result"]

    def get(self, key: str) -> dict[str, Any] | None:
        return self.values.get(key)

    def put(self, key: str, result: dict[str, Any]) -> None:
        with self.lock:
            if key in self.values:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"key": key, "result": result}, ensure_ascii=False) + "\n")
            self.values[key] = result


def record_key(title: str, description: str, tags: str, model: str) -> str:
    content = "\0".join((title, description, tags, model, "five-dimension-v1"))
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def process_record(
    index: int,
    record: dict[str, Any],
    client: OpenAICompatibleClient,
    cache: JsonlCache,
    overwrite: bool,
) -> tuple[int, dict[str, Any], dict[str, int | bool]]:
    output = dict(record)
    existing_result = pick_alias(record, ABILITY_KEYS)
    if not overwrite and existing_result:
        output[OUTPUT_FIELD] = existing_result
        return index, output, {"skipped": True, "cached": False, "total_tokens": 0}
    title = pick_alias(record, TITLE_KEYS)
    description = pick_alias(record, DESCRIPTION_KEYS)
    tags = pick_alias(record, TAGS_KEYS)
    if not description:
        raise ValueError(f"第 {index + 1} 条记录缺少 JD/职位描述。")
    evidence_text = "\n".join(part for part in (description, tags) if part)
    key = record_key(title, description, tags, client.model)
    cached = cache.get(key)
    usage = {"total_tokens": 0}
    if cached is None:
        raw_result, usage = client.extract(title, description, tags)
        result = validate_result(raw_result, evidence_text)
        cache.put(key, result)
    else:
        result = validate_result(cached, evidence_text)
    output[OUTPUT_FIELD] = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return index, output, {
        "skipped": False,
        "cached": cached is not None,
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="使用大模型从原始 JD 提取五维能力")
    parser.add_argument("--input", type=Path, required=True, help="CSV/JSON/JSONL 输入文件")
    parser.add_argument("--output", type=Path, required=True, help="输出 CSV/JSONL 文件")
    parser.add_argument(
        "--base-url",
        default=(
            read_environment("ABILITY_LLM_BASE_URL")
            or read_environment("IFLYTEK_SPARK_BASE_URL")
            or IFLYTEK_SPARK_BASE_URL
        ),
    )
    parser.add_argument(
        "--api-key",
        default=(
            read_environment("ABILITY_LLM_API_KEY")
            or read_environment("IFLYTEK_SPARK_API_PASSWORD")
            or read_environment("IFLYTEK_MAAS_API_KEY")
        ),
    )
    parser.add_argument(
        "--model",
        default=(
            read_environment("ABILITY_LLM_MODEL")
            or read_environment("IFLYTEK_SPARK_MODEL")
        ),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--response-mode",
        choices=("json-schema", "json-object", "prompt-only"),
        default="json-schema",
        help="模型结构化输出能力；不支持 response_format 时使用 prompt-only",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有能力提取结果")
    parser.add_argument("--cache", type=Path, help="断点缓存 JSONL；默认与输出文件同目录")
    args = parser.parse_args()

    source = args.input.resolve()
    destination = args.output.resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"输入文件不存在：{source}")
    if source.suffix.lower() not in {".csv", ".json", ".jsonl"}:
        raise ValueError("输入只支持 CSV、JSON 或 JSONL。")
    if destination.suffix.lower() not in {".csv", ".jsonl"}:
        raise ValueError("输出只支持 CSV 或 JSONL。")
    if source == destination:
        raise ValueError("输入与输出不能是同一文件，避免覆盖原始数据。")
    if not args.api_key:
        raise ValueError(
            "缺少讯飞星火 HTTP APIPassword；请设置 IFLYTEK_SPARK_API_PASSWORD，"
            "密钥不要写入命令行、代码或配置文件。"
        )
    if not args.base_url or not args.model:
        raise ValueError("请设置 IFLYTEK_SPARK_MODEL 为该 APIPassword 对应的模型 ID。")

    rows = list(iter_records(source))
    if args.limit:
        rows = rows[: max(0, args.limit)]
    if not rows:
        raise ValueError("输入文件没有有效记录。")
    fieldnames = list(rows[0])
    for row in rows[1:]:
        for field in row:
            if field not in fieldnames:
                fieldnames.append(field)
    if OUTPUT_FIELD not in fieldnames:
        fieldnames.append(OUTPUT_FIELD)

    cache_path = (args.cache or destination.with_suffix(destination.suffix + ".ability_cache.jsonl")).resolve()
    cache = JsonlCache(cache_path)
    client = OpenAICompatibleClient(
        args.base_url,
        args.api_key,
        args.model,
        args.timeout,
        args.retries,
        args.response_mode,
    )
    results: list[dict[str, Any] | None] = [None] * len(rows)
    metrics = {"rows": len(rows), "completed": 0, "skipped": 0, "cache_hits": 0, "failed": 0, "total_tokens": 0}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 32))) as executor:
        futures = {
            executor.submit(process_record, index, row, client, cache, args.overwrite): index
            for index, row in enumerate(rows)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                row_index, output, state = future.result()
                results[row_index] = output
                metrics["completed"] += int(not state["skipped"])
                metrics["skipped"] += int(bool(state["skipped"]))
                metrics["cache_hits"] += int(bool(state["cached"]))
                metrics["total_tokens"] += int(state["total_tokens"])
            except Exception as error:
                metrics["failed"] += 1
                errors.append(f"row={index + 1}: {type(error).__name__}: {error}")
            finished = metrics["completed"] + metrics["skipped"] + metrics["failed"]
            if finished % 50 == 0 or finished == len(rows):
                print(f"progress={finished}/{len(rows)} failed={metrics['failed']} cache={metrics['cache_hits']}", flush=True)

    if errors:
        error_path = destination.with_suffix(destination.suffix + ".errors.json")
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError(f"有 {len(errors)} 条提取失败；错误记录：{error_path}。修复后使用同一缓存重跑。")

    final_rows = [row for row in results if row is not None]
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if destination.suffix.lower() == ".csv":
        write_csv(temporary, final_rows, fieldnames)
    else:
        write_jsonl(temporary, final_rows)
    temporary.replace(destination)
    report = destination.with_suffix(destination.suffix + ".report.json")
    report.write_text(
        json.dumps(
            {
                **metrics,
                "input": str(source),
                "output": str(destination),
                "cache": str(cache_path),
                "model": args.model,
                "provider": "iflytek_spark_openai",
                "base_url": args.base_url,
                "response_mode": args.response_mode,
                "dimensions": list(DIMENSIONS),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"EXTRACTION_COMPLETE output={destination} report={report}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已中断；已完成结果仍保存在缓存中，下次可继续。", file=sys.stderr)
        raise SystemExit(130)
