"""FastAPI HTTP 封装 — 简历人岗匹配分析（前端联调用）。

路由：/health /upload /extract /rank /enhance /gap /modify /radar
所有业务逻辑复用 src/tools/，本文件只做平台边界（请求解析 / 响应序列化）。

运行（本地，默认 http://127.0.0.1:8000，Swagger 文档 /docs）：
    python api_server.py
    uvicorn api_server:app --host 0.0.0.0 --port 8000

说明：
- /health /upload /rank /radar 为纯逻辑，不调用 LLM；
- /extract /enhance /gap /modify 需要 LLM，服务进程启动时从项目 .env
  读取 LLM_PROVIDER 对应供应商的配置块（如 DEEPSEEK_API_KEY，未配置时返回 400 并提示）。
"""

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, SecretStr

from src.store import create_store
from src.tools.analyze import analyze_gap
from src.tools.enhance import enhance_matches
from src.tools.modify import suggest_resume_edit, validate_resume_edit
from src.tools.rank import rank_resume
from src.tools.resume_extract import extract_resume_profile
from src.tools.visualize import render_radar
from src.utils.llm import LLM_PROVIDERS, call_llm_json, get_llm_config
from src.utils.text import convert_to_markdown


app = FastAPI(
    title="简历人岗匹配分析 HTTP API",
    version="1.0.0",
    description=(
        "简历岗位匹配分析：/upload 转 Markdown → /extract 7 维画像 → "
        "/rank 关键词粗排 → /enhance 语义复核 → /gap 差距分析 → "
        "/modify 修改建议 → /radar 七维雷达图。"
    ),
)

# 前端本地联调：允许跨域（部署时按需收紧 allow_origins）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class HealthResponse(BaseModel):
    status: str
    service: str = "resume-analysis-api"
    store_backend: str
    roles_available: int
    llm_configured: bool


class LlmRequest(BaseModel):
    """仅用于当前请求的模型配置，不写入环境变量或磁盘。"""

    provider: str = Field("deepseek", description="deepseek/iflytek/openai/custom")
    api_key: SecretStr = Field(..., description="仅用于当前请求的 API Key")
    model: str = Field("", description="模型名；留空使用供应商默认值")
    base_url: str = Field("", description="兼容 OpenAI 的服务地址；custom 必填")


class ExtractRequest(BaseModel):
    resume_text: str = Field(..., description="简历 Markdown 原文（/upload 返回的 text）")
    position: str = Field("", description="目标岗位（可选，缺省按简历求职意向）")
    llm: Optional[LlmRequest] = Field(None, description="可选的单次请求模型配置")


class RankRequest(BaseModel):
    resume_text: str = Field(..., description="简历 Markdown 原文")
    topk: int = Field(10, ge=1, le=200, description="返回前 N 名")


class EnhanceRequest(BaseModel):
    rank_result: Dict[str, Any] = Field(..., description="/rank 返回的完整 JSON")
    resume_text: str = Field(..., description="简历 Markdown 原文")
    topk: int = Field(20, ge=1, le=200, description="复核前 N 名")
    llm: Optional[LlmRequest] = Field(None, description="可选的单次请求模型配置")


class GapRequest(BaseModel):
    role: Dict[str, Any] = Field(..., description="rank/enhance 结果中的单个 role JSON")
    resume_text: str = Field(..., description="简历 Markdown 原文")
    llm: Optional[LlmRequest] = Field(None, description="可选的单次请求模型配置")


class ModifyRequest(BaseModel):
    role: Dict[str, Any] = Field(..., description="rank/enhance 结果中的单个 role JSON")
    resume_text: str = Field(..., description="简历 Markdown 原文")
    llm: Optional[LlmRequest] = Field(None, description="可选的单次请求模型配置")


class RadarRequest(BaseModel):
    role: Dict[str, Any] = Field(..., description="rank/enhance 结果中的单个 role JSON（含 dimensions.coverage）")
    role_name: str = Field("", description="雷达图标题岗位名（可选）")


def _request_llm_caller(
    request: Optional[LlmRequest],
) -> Optional[Callable[[str], Dict[str, Any]]]:
    """把前端传入配置转换为线程安全的单次调用函数。"""
    if request is None:
        return None

    provider = request.provider.strip().lower()
    if provider not in LLM_PROVIDERS:
        raise ValueError(
            f"未知 LLM provider: {provider}，可选: {', '.join(sorted(LLM_PROVIDERS))}"
        )
    preset = LLM_PROVIDERS[provider]
    base_url = request.base_url.strip() or preset.get("base_url")
    model = request.model.strip() or preset.get("default_model")
    if not base_url:
        raise ValueError("custom provider 必须提供 base_url")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    if not model:
        raise ValueError("custom provider 必须提供 model")

    cfg: Dict[str, Any] = {
        "provider": provider,
        "label": preset["label"],
        "api_key": request.api_key.get_secret_value(),
        "model": model,
        "base_url": base_url,
        "extra_body": None,
        "timeout": float(os.getenv("LLM_TIMEOUT", "60")),
        "max_retries": int(os.getenv("LLM_MAX_RETRIES", "2")),
    }
    return lambda prompt: call_llm_json(prompt, cfg=cfg)


@app.exception_handler(ValueError)
async def value_error_handler(_, exc: ValueError) -> JSONResponse:
    """业务参数/配置错误 → 400。"""
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(RuntimeError)
async def runtime_error_handler(_, exc: RuntimeError) -> JSONResponse:
    """LLM 调用失败 / 数据源异常 → 500。"""
    return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/", include_in_schema=False)
def index() -> Dict[str, Any]:
    return {
        "service": "resume-analysis-api",
        "docs": "/docs",
        "endpoints": [
            "/health",
            "/upload",
            "/extract",
            "/rank",
            "/enhance",
            "/gap",
            "/modify",
            "/radar",
        ],
    }


@app.get("/health", response_model=HealthResponse, summary="健康检查 / 数据源状态")
def health() -> HealthResponse:
    """返回服务状态、当前数据源后端、可匹配岗位数与 LLM 配置情况。"""
    backend = os.getenv("STORE_BACKEND", "memory")
    try:
        store = create_store(backend)
        roles = store.get_all_roles()
        roles_available = len(roles)
    except Exception:
        roles_available = 0
    try:
        get_llm_config()
        llm_configured = True
    except Exception:
        llm_configured = False
    return HealthResponse(
        status="ok" if roles_available else "degraded",
        store_backend=backend,
        roles_available=roles_available,
        llm_configured=llm_configured,
    )


@app.post("/upload", summary="上传简历并转换为 Markdown 文本")
async def upload_resume(file: UploadFile = File(...)) -> Dict[str, Any]:
    """接收 PDF/DOCX/MD/TXT 简历文件，返回 Markdown 原文供后续接口使用。"""
    suffix = Path(file.filename or "resume.txt").suffix.lower()
    if suffix not in {".pdf", ".docx", ".doc", ".md", ".markdown", ".txt"}:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {suffix}（支持 PDF/DOCX/MD/TXT）",
        )
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix="resume_upload_"
        ) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name
        text = convert_to_markdown(tmp_path)
    finally:
        if tmp_path:
            os.unlink(tmp_path)
    return {"filename": file.filename, "text": text}


@app.post("/extract", summary="简历 → 7 维画像（LLM）")
def extract_resume(req: ExtractRequest) -> Dict[str, Any]:
    """提取候选人 7 维画像并做防幻觉校验（需要 LLM 凭证）。"""
    return extract_resume_profile(
        req.resume_text,
        req.position or None,
        llm_func=_request_llm_caller(req.llm),
    )


@app.post("/rank", summary="关键词命中粗排 Top-N")
def rank(req: RankRequest) -> Dict[str, Any]:
    """简历原文 vs 全部 Role 的七维覆盖率粗排（纯逻辑，不调 LLM）。"""
    return rank_resume(req.resume_text, topk=req.topk)


@app.post("/enhance", summary="语义复核粗排结果（LLM）")
def enhance(req: EnhanceRequest) -> Dict[str, Any]:
    """LLM 复核 Top-N 命中，修正误判并重算覆盖率（需要 LLM 凭证）。"""
    return enhance_matches(
        req.rank_result,
        req.resume_text,
        topk=req.topk,
        llm_func=_request_llm_caller(req.llm),
    )


@app.post("/gap", summary="单岗位差距分析 + 学习路径（LLM）")
def gap(req: GapRequest) -> Dict[str, Any]:
    """对单个 role 做七维差距分析，返回 analysis（JSON）+ markdown 报告。"""
    return analyze_gap(
        req.role,
        req.resume_text,
        llm_func=_request_llm_caller(req.llm),
    )


@app.post("/modify", summary="简历修改建议 + 防造假校验（LLM）")
def modify(req: ModifyRequest) -> Dict[str, Any]:
    """生成针对目标岗位的简历修改建议，并附 validate_resume_edit 校验报告。"""
    result = suggest_resume_edit(
        req.role,
        req.resume_text,
        llm_func=_request_llm_caller(req.llm),
    )
    validation = validate_resume_edit(req.role, req.resume_text, result["analysis"])
    result["validation"] = validation
    return result


@app.post("/radar", summary="七维雷达图 PNG")
def radar(req: RadarRequest) -> FileResponse:
    """渲染单个 role 的七维雷达图，直接返回 PNG 图片。"""
    path = render_radar(req.role, req.role_name)
    return FileResponse(
        path,
        media_type="image/png",
        filename=f"radar_{req.role_name or req.role.get('role_name', 'role')}.png",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
