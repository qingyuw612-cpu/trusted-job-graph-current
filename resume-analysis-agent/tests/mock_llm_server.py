"""本地 mock LLM 服务（OpenAI 兼容协议）— 用于 API/集成测试与前端本地联调。

按提示词内容返回对应路由的合法 JSON：
- 简历提取（extract）：返回 7 维画像
- 语义复核（enhance）：回显输入 rank JSON 并加 review_note
- 差距分析（gap）：返回 7 维 gap JSON + 学习路径
- 简历修改（modify）：返回修改建议 JSON

用法（另开终端，起在 9009 端口）：
    python tests/mock_llm_server.py

然后启动 API 服务并指向 mock（Switch 模式：用 CUSTOM 配置块）：
    $env:LLM_PROVIDER="custom"
    $env:CUSTOM_API_KEY="mock"
    $env:CUSTOM_BASE_URL="http://127.0.0.1:9009/v1"
    $env:CUSTOM_MODEL="mock-model"
    python api_server.py
"""

import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer


def _extract_json_block(text: str, start_marker: str) -> str:
    """在提示词中找到 start_marker 之后的第一个完整 JSON 对象。"""
    idx = text.find(start_marker)
    if idx == -1:
        return ""
    start = text.find("{", idx)
    if start == -1:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _route(prompt: str) -> dict:
    if "根据胜任力模型" in prompt or "Extract the candidate" in prompt:
        # extract-resume：返回 7 维画像（条目需能在测试简历原文中找到）
        return {
            "position": "机器学习工程师",
            "dimensions": {
                "knowledge": ["深度学习训练", "图像分类项目"],
                "skill": ["Python", "PyTorch"],
                "qualifications": ["本科学历", "计算机专业"],
                "preference": [],
                "motivation": [],
                "trait": [],
                "self_concept": [],
            },
        }
    if "## Keyword Match Results (JSON)" in prompt:
        # enhance：回显输入 rank JSON，逐条加 review_note
        block = _extract_json_block(prompt, "## Keyword Match Results (JSON)")
        rank = json.loads(block) if block else {"topk": 0, "results": []}
        for item in rank.get("results", []):
            item["review_note"] = "mock: 无修正"
        return rank
    if "## Pre-computed Match Details" in prompt:
        # gap：返回 7 维差距分析 + 学习路径
        dims = {k: {"gap_level": "sufficient", "summary": "mock 分析"} for k in
                ("knowledge", "skill", "qualifications", "preference",
                 "motivation", "trait", "self_concept")}
        return {
            "match": {"verdict": "yes", "reason": "mock：核心技能充分"},
            "dimensions": dims,
            "overall_summary": "mock 总体建议",
            "learning_path": [
                {"step": 1, "skill": "mock 技能", "importance": "high"}
            ],
        }
    # modify：返回修改建议
    return {
        "target_role": "mock 岗位",
        "summary": "mock 总体结论",
        "suggestions": [
            {
                "skill": "Java",
                "status": "hit",
                "suggestion": "mock 建议：保持现有表述",
                "example_rewrite": "",
            }
        ],
        "risks": [],
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        prompt = body
        try:
            payload = json.loads(body)
            for m in reversed(payload.get("messages", [])):
                if m.get("role") == "user":
                    prompt = m.get("content", "")
                    break
        except Exception:
            pass
        content = _route(prompt)
        resp = {
            "choices": [
                {"message": {"role": "assistant",
                             "content": json.dumps(content, ensure_ascii=False)}}
            ]
        }
        data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 9009), Handler).serve_forever()

