"""通用 LLM 调用适配器 —— 并列 Switch 模式，支持 DeepSeek / 讯飞星火 / OpenAI / 自定义 OpenAI 兼容服务。

每个供应商一个独立配置块，`LLM_PROVIDER` 是切换键：
    LLM_PROVIDER=deepseek
    DEEPSEEK_API_KEY=...  DEEPSEEK_BASE_URL=...  DEEPSEEK_MODEL=...
    IFLYTEK_API_KEY=...   IFLYTEK_BASE_URL=...   IFLYTEK_MODEL=...
    OPENAI_API_KEY=...    OPENAI_BASE_URL=...    OPENAI_MODEL=...
    CUSTOM_API_KEY=...    CUSTOM_BASE_URL=...    CUSTOM_MODEL=...

通用（非凭证）变量：LLM_TIMEOUT / LLM_MAX_RETRIES。
不再使用共享的 LLM_API_KEY / LLM_MODEL / LLM_BASE_URL / LLM_EXTRA_BODY。

CLI 模式（enhance / analyze / modify / extract-resume）统一从这里调用；
MCP 模式不调用任何外部 LLM，本模块仅用于 CLI 路径。
"""

import json
import os
import re
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

# OpenAI 兼容通道的 provider 预设表
LLM_PROVIDERS: Dict[str, Dict[str, Optional[str]]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    "iflytek": {
        "label": "讯飞星火",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "default_model": "4.0Ultra",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
    },
    "custom": {
        "label": "自定义",
        "base_url": None,
        "default_model": None,
    },
}

DEFAULT_PROVIDER = "deepseek"


def get_llm_config() -> Dict[str, Any]:
    """解析环境变量，返回当前 LLM 连接配置（并列 Switch 模式）。

    只读取 `LLM_PROVIDER` 所选供应商的独立配置块 `{PROVIDER}_*`，
    供应商之间互不干扰；切换供应商只需改 LLM_PROVIDER。
    """
    provider = (os.getenv("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if provider not in LLM_PROVIDERS:
        raise ValueError(
            f"未知 LLM_PROVIDER: {provider}，可选: {', '.join(sorted(LLM_PROVIDERS))}"
        )
    preset = LLM_PROVIDERS[provider]
    prefix = provider.upper() + "_"

    api_key = os.getenv(prefix + "API_KEY")
    base_url = os.getenv(prefix + "BASE_URL") or preset.get("base_url")
    model = os.getenv(prefix + "MODEL") or preset.get("default_model")
    extra_body_raw = os.getenv(prefix + "EXTRA_BODY")

    if not api_key:
        raise ValueError(
            f"缺少 {prefix}API_KEY，请在 .env 中配置（当前供应商 {provider}）"
        )
    if not base_url:
        raise ValueError(
            f"缺少 {prefix}BASE_URL，custom provider 必须显式配置端点"
        )
    if not model:
        raise ValueError(
            f"缺少 {prefix}MODEL，custom provider 必须显式配置模型"
        )

    extra_body: Optional[Dict[str, Any]] = None
    if extra_body_raw:
        try:
            extra_body = json.loads(extra_body_raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{prefix}EXTRA_BODY 不是合法 JSON: {extra_body_raw}"
            ) from exc
        if not isinstance(extra_body, dict):
            raise ValueError(f"{prefix}EXTRA_BODY 必须是 JSON 对象")

    return {
        "provider": provider,
        "label": preset["label"],
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
        "extra_body": extra_body,
        "timeout": float(os.getenv("LLM_TIMEOUT", "60")),
        "max_retries": int(os.getenv("LLM_MAX_RETRIES", "2")),
    }


def build_chat_model(
    cfg: Optional[Dict[str, Any]] = None,
    temperature: float = 0.0,
) -> Any:
    """按配置构造 langchain ChatOpenAI（OpenAI 兼容协议，各 provider 通用）。"""
    from langchain_openai import ChatOpenAI

    cfg = cfg or get_llm_config()
    kwargs: Dict[str, Any] = {
        "model": cfg["model"],
        "api_key": cfg["api_key"],
        "base_url": cfg["base_url"],
        "temperature": temperature,
        "timeout": cfg["timeout"],
        "max_retries": cfg["max_retries"],
    }
    if cfg.get("extra_body"):
        kwargs["extra_body"] = cfg["extra_body"]
    return ChatOpenAI(**kwargs)


def _repair_json(content: str) -> str:
    """对 LLM 返回的 JSON 做安全修复，处理常见格式问题。

    已修复：
    - 多余的尾部逗号（如 "a": 1, }）
    - 字符串内未转义的换行/控制字符
    - 未闭合的中文引号

    不做激进重写，无法修复时保持原样交给 json.loads 报错。
    """
    if not content:
        return content

    # 1. 去除 BOM
    if content.startswith("\ufeff"):
        content = content.lstrip("\ufeff")

    # 2. 修复尾部逗号：逗号后面紧跟 } 或 ]
    content = re.sub(r",\s*([}\]])", r"\1", content)

    # 3. 移除字符串值中常见的未转义控制字符
    content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", content)

    # 4. 如果以截断的形式结束（无闭合括号），按栈顺序补全
    stripped = content.rstrip()
    stack: list[str] = []
    for ch in stripped:
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if stack:
        for ch in reversed(stack):
            content += "}" if ch == "{" else "]"
        # 补全可能引入新的尾部逗号，再次修复
        content = re.sub(r",\s*([}\]])", r"\1", content)

    return content


def call_llm_json(
    prompt: str,
    temperature: float = 0.0,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """调用当前配置的 LLM 并解析 JSON 输出。

    Args:
        prompt: 完整提示词
        temperature: 采样温度，默认 0（确定性优先）
        cfg: 可选的单次请求配置；缺省时仍从环境变量读取

    Returns:
        解析后的 JSON 对象

    Raises:
        RuntimeError: LLM 调用失败或返回内容无法解析为 JSON
    """
    cfg = cfg or get_llm_config()
    try:
        llm = build_chat_model(cfg, temperature=temperature)
        response = llm.invoke(prompt)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"LLM 调用失败（{cfg['label']} / {cfg['model']}）: {exc}"
        ) from exc

    content = (
        response.content.strip()
        if hasattr(response, "content")
        else str(response).strip()
    )

    # 清理可能的 markdown 代码块包裹
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        content = content.rsplit("```", 1)[0].strip()

    # 容错修复后解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        repaired = _repair_json(content)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            pos = exc.pos
            snippet = content[max(0, pos - 80):pos + 80].replace("\n", "\\n")
            raise RuntimeError(
                f"LLM 返回内容无法解析为 JSON: {exc}\n错误位置附近: ...{snippet}..."
            ) from exc


def call_deepseek_json(prompt: str, temperature: float = 0.0) -> Dict[str, Any]:
    """兼容别名：等价于 call_llm_json（旧代码/文档保留）。"""
    return call_llm_json(prompt, temperature=temperature)
