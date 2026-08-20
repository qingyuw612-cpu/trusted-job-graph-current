"""简历结构化画像提取 —— LLM 版（7 维 schema，批量标准化输出）。

双模式：
- Agent 模式：prepare_resume_extract 返回提示包，Agent 用自己的模型输出画像，
  再用 validate_resume_profile 做纯逻辑校验；
- CLI 模式：extract_resume_profile / extract_resume_batch 直接调用当前 LLM
  （call_llm_json，默认 DeepSeek，可切讯飞星火等）。
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..core.dimensions import DIMENSION_KEYS
from ..prompts.resume_extract import RESUME_EXTRACT_PROMPT
from ..utils.llm import call_llm_json
from ..utils.text import convert_to_markdown

# 中文维度名 → 项目 7 维 key（兼容 LLM 按提示词原样输出中文的情况）
DIMENSION_ALIASES = {
    "知识": "knowledge",
    "技术": "skill",
    "技能": "skill",
    "任职条件": "qualifications",
    "招聘偏好": "preference",
    "动机": "motivation",
    "特质": "trait",
    "自我概念": "self_concept",
}

MAX_ITEM_LEN = 30


def _normalize_text(text: str) -> str:
    """去空白/常见标点/大小写，用于子串包含判断。"""
    if not text:
        return ""
    return re.sub(
        r"[\s，。；、,.!?！？:：;；\"'“”‘’()（）\[\]【】\-—_/\\|*#`]+",
        "",
        text,
    ).lower()


def _normalize_item(item: Any) -> Optional[str]:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("name", "item", "value", "skill"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _grounded(item_norm: str, text_norm: str) -> bool:
    """条目是否在原文中有连续字面依据。

    短词（<=4 字）要求完全包含；长词要求至少存在一个连续 4 字窗口
    命中原文（容忍“数据库设计与优化” → “数据库设计与优化知识”这类
    类别词/连接词的轻度改写，同时拦截完全虚构的内容）。
    """
    if not item_norm:
        return False
    if len(item_norm) <= 4:
        return item_norm in text_norm
    return any(
        text_norm.find(item_norm[i:i + 4]) >= 0
        for i in range(len(item_norm) - 3)
    )


def build_resume_profile(
    resume_text: str,
    position: str,
    llm_result: Dict[str, Any],
    max_item_len: int = MAX_ITEM_LEN,
) -> Dict[str, Any]:
    """把 LLM 输出规范化为 7 维画像（纯逻辑，不调用 LLM）。

    兼容：dimensions 嵌套在 llm_result["dimensions"]，或直接平铺在顶层。
    超过 max_item_len 的条目自动截断，并记录 truncations 供审计标注。
    """
    raw_dims = llm_result.get("dimensions")
    if not isinstance(raw_dims, dict):
        raw_dims = {
            k: v
            for k, v in llm_result.items()
            if k in DIMENSION_ALIASES or k in DIMENSION_KEYS
        }
    if not isinstance(raw_dims, dict):
        raise ValueError("LLM 输出缺少 dimensions 对象")

    dims: Dict[str, List[str]] = {}
    truncations: List[Dict[str, str]] = []
    for key, items in raw_dims.items():
        dim = DIMENSION_ALIASES.get(str(key), str(key))
        if dim not in DIMENSION_KEYS:
            continue
        cleaned: List[str] = []
        if isinstance(items, list):
            for item in items:
                norm = _normalize_item(item)
                if norm and norm not in cleaned:
                    if len(norm) > max_item_len:
                        truncations.append(
                            {
                                "dim": dim,
                                "original": norm,
                                "truncated": norm[:max_item_len],
                            }
                        )
                        norm = norm[:max_item_len]
                        if norm in cleaned:
                            continue
                    cleaned.append(norm)
        dims[dim] = cleaned

    for key in DIMENSION_KEYS:
        dims.setdefault(key, [])

    return {
        "position": position or llm_result.get("position") or "",
        "dimensions": dims,
        "truncations": truncations,
    }


def validate_resume_profile(
    profile: Dict[str, Any],
    resume_text: str,
    max_item_len: int = MAX_ITEM_LEN,
) -> Dict[str, Any]:
    """纯逻辑校验：维度白名单 / 条目必须来自原文 / 去重 / 长度上限。

    返回 {"ok", "violations", "stats"}。
    """
    violations: List[str] = []
    dims = profile.get("dimensions") or {}
    normalized_text = _normalize_text(resume_text)

    for dim in DIMENSION_KEYS:
        items = dims.get(dim)
        if items is None:
            violations.append(f"{dim}: 缺少维度")
            continue
        if not isinstance(items, list):
            violations.append(f"{dim}: 应为数组")
            continue
        seen = set()
        for item in items:
            if not isinstance(item, str) or not item.strip():
                violations.append(f"{dim}: 存在空条目")
                continue
            if len(item) > max_item_len:
                violations.append(f"{dim}: 条目过长（>{max_item_len} 字）: {item[:20]}...")
            norm = _normalize_text(item)
            if norm in seen:
                violations.append(f"{dim}: 重复条目: {item}")
            seen.add(norm)
            if norm and not _grounded(norm, normalized_text):
                violations.append(f"{dim}: 条目缺乏原文依据: {item}")

    stats = {
        "total_items": sum(len(dims.get(k) or []) for k in DIMENSION_KEYS),
        "dim_coverage": {k: len(dims.get(k) or []) for k in DIMENSION_KEYS},
    }
    return {"ok": not violations, "violations": violations, "stats": stats}


def prepare_resume_extract(
    resume_text: str,
    position: Optional[str] = None,
) -> Dict[str, Any]:
    """Agent 模式：构建简历提取提示包，不调用 LLM API。"""
    text = (resume_text or "").strip()
    if not text:
        raise ValueError("resume_text 不能为空")
    prompt = RESUME_EXTRACT_PROMPT.format(
        position=position or "未知（按简历求职意向）",
        resume_text=text[:12000],
    )
    return {
        "mode": "agent_extract",
        "purpose": "Extract the candidate's 7-dimension capability profile from the resume with your own model",
        "prompt": prompt,
        "resume_text": text[:12000],
        "position": position or "",
        "output_schema": {
            "position": "目标岗位",
            "dimensions": {
                "knowledge": ["知识短语"],
                "skill": ["技术短语"],
                "qualifications": ["任职条件短语"],
                "preference": ["招聘偏好短语"],
                "motivation": ["动机短语"],
                "trait": ["特质短语"],
                "self_concept": ["自我概念短语"],
            },
        },
        "next_step": "run validate_resume_profile(profile, resume_text)",
    }


def extract_resume_profile(
    resume_text: str,
    position: Optional[str] = None,
    llm_func: Optional[Any] = None,
) -> Dict[str, Any]:
    """CLI 模式：调用 LLM 提取单份简历的 7 维画像，并做纯逻辑校验。"""
    text = (resume_text or "").strip()
    if not text:
        raise ValueError("resume_text 不能为空")
    payload = prepare_resume_extract(text, position)
    caller = llm_func or call_llm_json
    llm_result = caller(payload["prompt"])
    if not isinstance(llm_result, dict):
        raise RuntimeError("LLM 返回格式异常：预期 JSON 对象")
    return apply_resume_extract(text, llm_result, position or "")


def apply_resume_extract(
    resume_text: str,
    llm_result: Dict[str, Any],
    position: str = "",
) -> Dict[str, Any]:
    """Agent 模式：把 Agent 用自己的模型产出的 JSON 规范化并校验（纯逻辑，不调用 LLM）。

    返回 {"position", "dimensions", "truncations", "stats", "validation"}，
    其中 stats.truncated 为超长条目自动截断数量（含 truncations 审计明细）。
    """
    text = (resume_text or "").strip()
    if not text:
        raise ValueError("resume_text 不能为空")
    if not isinstance(llm_result, dict):
        raise ValueError("extract_json invalid: expected an object")
    profile = build_resume_profile(text, position, llm_result)
    validation = validate_resume_profile(profile, text)
    truncations = profile.get("truncations") or []
    validation["stats"]["truncated"] = len(truncations)
    profile["stats"] = validation["stats"]
    profile["validation"] = validation
    return profile


def load_resume_items(source: str) -> List[Dict[str, Any]]:
    """把简历文件夹或 JSON 数组文件加载为统一条目列表。

    每条: {"file_name", "source", "text", "position"}
    JSON 数组兼容 faircv 格式（{"content", "metadata": {"position"}}）。
    """
    path = Path(source)
    items: List[Dict[str, Any]] = []

    def _append(file_name: str, text: str, position: Optional[str]) -> None:
        if text and text.strip():
            items.append(
                {
                    "file_name": file_name,
                    "source": str(path),
                    "text": text,
                    "position": position,
                }
            )

    if path.is_dir():
        exts = {".pdf", ".docx", ".md", ".txt"}
        for f in sorted(
            p for p in path.iterdir() if p.is_file() and p.suffix.lower() in exts
        ):
            _append(f.name, convert_to_markdown(str(f)), None)
    elif path.is_file() and path.suffix.lower() == ".json":
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("JSON 文件应为数组（faircv 格式）")
        for i, rec in enumerate(data):
            if not isinstance(rec, dict):
                continue
            meta = rec.get("metadata") or {}
            _append(
                rec.get("file_name") or f"item_{i:03d}.json",
                rec.get("content") or rec.get("raw_text") or "",
                meta.get("position"),
            )
    elif path.is_file():
        _append(path.name, convert_to_markdown(str(path)), None)
    else:
        raise FileNotFoundError(f"输入不存在: {source}")
    return items


def extract_resume_batch(
    items: List[Dict[str, Any]],
    llm_func: Optional[Any] = None,
    position: Optional[str] = None,
    max_workers: int = 1,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """批量提取：单条失败隔离，不中断整批；输出主 JSON + 覆盖率统计。

    max_workers > 1 时用线程池并发调用 LLM（API 调用是 IO 密集，适合多线程）。
    progress_cb(done, total) 可选回调，用于 CLI 进度展示。
    """
    total = len(items)

    def _run_one(index: int) -> tuple[int, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        item = items[index]
        file_name = item.get("file_name") or f"item_{index + 1}"
        try:
            profile = extract_resume_profile(
                item.get("text", ""),
                position=position or item.get("position"),
                llm_func=llm_func,
            )
        except Exception as exc:  # noqa: BLE001 - 失败隔离，单条失败不中断整批
            return index, None, {"file_name": file_name, "error": str(exc)}
        return index, {
                "file_name": file_name,
                "source": item.get("source", ""),
                "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                **profile,
            }, None

    if max_workers <= 1:
        raw_results = [_run_one(i) for i in range(total)]
    else:
        raw_results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_run_one, i) for i in range(total)]
            for done in as_completed(futures):
                raw_results.append(done.result())
                if progress_cb:
                    progress_cb(len(raw_results), total)
    # 按原输入顺序还原，保证输出稳定可复现
    raw_results.sort(key=lambda r: r[0])

    profiles: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for _, profile, err in raw_results:
        if err is not None:
            errors.append(err)
        else:
            profiles.append(profile)

    coverage = {
        k: sum(1 for p in profiles if p.get("dimensions", {}).get(k))
        for k in DIMENSION_KEYS
    }
    return {
        "total": total,
        "ok": len(profiles),
        "failed": len(errors),
        "errors": errors,
        "profiles": profiles,
        "coverage": coverage,
    }

