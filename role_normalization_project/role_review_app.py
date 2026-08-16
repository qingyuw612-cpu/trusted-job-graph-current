"""零依赖本地岗位审核页面，只监听127.0.0.1。"""
from __future__ import annotations

import argparse
import csv
import html
import os
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from concept_standardization.engine import ConceptStandardizationEngine, DECISIONS


PROJECT = Path(__file__).resolve().parent
WORKSPACE = PROJECT.parents[1]
DEFAULT_QUEUE = WORKSPACE / "2026数据51job" / "岗位概念标准化结果" / "concept_review_queue.csv"


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def save_rows(path: Path, rows: list[dict[str, str]]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=ConceptStandardizationEngine.REVIEW_FIELDS, extrasaction="ignore")
            writer.writeheader(); writer.writerows(rows)
        os.replace(temp_name, path)
    except Exception:
        try: os.unlink(temp_name)
        except OSError: pass
        raise


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def page(row: dict[str, str], index: int, total: int, message: str = "") -> bytes:
    ai_decision = row.get("ai_decision") or ""
    ai_role = row.get("ai_target_role_id") or ""
    ai_name = row.get("ai_canonical_name") or ""
    ai_parent = row.get("ai_parent_role_id") or ""
    ai_tags = row.get("ai_tags") or ""
    ai_reason = row.get("ai_reason") or ""
    suggested = row.get("suggested_decision") or "INSUFFICIENT_INFO"
    use_ai = bool(ai_decision)
    default_decision = ai_decision if use_ai else suggested
    default_role = ai_role if use_ai else row.get("suggested_role_id", "")
    default_name = ai_name if use_ai else row.get("suggested_role_name", "")
    default_parent = ai_parent
    default_tags = ai_tags
    default_note = ai_reason if use_ai else row.get("suggested_reason", "")
    options = "".join(f'<option value="{esc(x)}" {"selected" if x == default_decision else ""}>{esc(x)}</option>' for x in sorted(DECISIONS))
    prev_q = urlencode({"i": max(0, index - 1)})
    next_q = urlencode({"i": min(total - 1, index + 1)})
    body = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>岗位概念审核</title>
<style>body{{font-family:Arial,'Microsoft YaHei';margin:0;background:#f5f7fa;color:#263238}}header{{background:#183153;color:white;padding:14px 24px;position:sticky;top:0}}main{{max-width:1100px;margin:20px auto;padding:0 16px}}.card{{background:white;border-radius:10px;padding:18px;margin:12px 0;box-shadow:0 2px 10px #0001}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}label{{display:block;font-size:13px;color:#607d8b;margin-top:9px}}input,select,textarea{{width:100%;box-sizing:border-box;padding:9px;border:1px solid #ccd5df;border-radius:6px}}textarea{{min-height:90px}}button,.btn{{display:inline-block;padding:10px 16px;border:0;border-radius:6px;text-decoration:none;cursor:pointer}}button{{background:#1677ff;color:white}}.approve{{background:#14804a}}.observe{{background:#866118}}.nav{{background:#e9eef5;color:#183153}}.tag{{display:inline-block;background:#eef4ff;padding:4px 8px;border-radius:12px;margin:3px}}.msg{{background:#e8fff1;padding:10px;border-radius:6px;color:#126338}}small{{color:#667}}</style></head>
<body><header><b>岗位概念审核</b>　{index + 1}/{total}　状态：{esc(row.get('review_status'))}</header><main>
{f'<div class="msg">{esc(message)}</div>' if message else ''}
<div><a class="btn nav" href="/?{prev_q}">← 上一个</a> <a class="btn nav" href="/?{next_q}">下一个 →</a></div>
<div class="card"><h2>{esc(row.get('source_name'))}</h2><span class="tag">JD {esc(row.get('jd_count'))}</span><span class="tag">企业 {esc(row.get('company_count'))}</span><span class="tag">证据 {esc(row.get('evidence_level'))}</span>
<p><b>常见原始名称：</b>{esc(row.get('top_original_names'))}</p><p><b>核心技能：</b>{esc(row.get('top_skills'))}</p></div>
<div class="grid"><div class="card"><h3>算法建议</h3><p>{esc(row.get('suggested_decision'))} → {esc(row.get('suggested_role_name'))}</p><p>置信度：{esc(row.get('suggested_confidence'))}</p><small>{esc(row.get('suggested_reason'))}</small><p>候选：{esc(row.get('candidate_1'))} ({esc(row.get('candidate_1_score'))})；{esc(row.get('candidate_2'))} ({esc(row.get('candidate_2_score'))})</p></div>
<div class="card"><h3>AI建议</h3><p>{esc(ai_decision or '尚未导入AI结果')} → {esc(ai_name)}</p><p>置信度：{esc(row.get('ai_confidence'))}</p><small>{esc(ai_reason)}</small></div></div>
<form class="card" method="post" action="/save"><input type="hidden" name="candidate_id" value="{esc(row.get('candidate_id'))}"><input type="hidden" name="i" value="{index}"><h3>人工结论</h3>
<div class="grid"><div><label>决策</label><select name="decision">{options}</select></div><div><label>目标已有岗位ID</label><input name="role_id" value="{esc(default_role)}"></div>
<div><label>标准岗位名称（新岗位必填）</label><input name="canonical_name" value="{esc(default_name)}"></div><div><label>上级岗位ID</label><input name="parent_role_id" value="{esc(default_parent)}"></div></div>
<label>方向标签（用；分隔）</label><input name="tags" value="{esc(default_tags)}"><label>审核依据</label><textarea name="note">{esc(default_note)}</textarea>
<p><button class="approve" name="action" value="approve">批准并进入下一个</button> <button class="observe" name="action" value="observe">保留观察</button> <button name="action" value="save">仅保存</button></p></form>
</main></body></html>"""
    return body.encode("utf-8")


def make_handler(queue_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            all_rows = load_rows(queue_path)
            rows = [row for row in all_rows if row.get("ai_decision")] or all_rows
            query = parse_qs(urlparse(self.path).query)
            index = max(0, min(len(rows) - 1, int(query.get("i", ["0"])[0])))
            content = page(rows[index], index, len(rows), query.get("msg", [""])[0])
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(content)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8")).items()}
            rows = load_rows(queue_path)
            index = int(form.get("i", "0")); target = form.get("candidate_id", "")
            row = next((x for x in rows if x.get("candidate_id") == target), None)
            if row is None:
                self.send_error(404, "candidate not found"); return
            row.update({"reviewer_decision": form.get("decision", ""), "reviewer_role_id": form.get("role_id", ""),
                        "reviewer_canonical_name": form.get("canonical_name", ""),
                        "reviewer_parent_role_id": form.get("parent_role_id", ""), "reviewer_tags": form.get("tags", ""),
                        "reviewer_note": form.get("note", "")})
            action = form.get("action", "save")
            row["review_status"] = "APPROVED" if action == "approve" else ("PENDING" if action == "save" else "OBSERVE")
            save_rows(queue_path, rows)
            visible_count = sum(bool(x.get("ai_decision")) for x in rows) or len(rows)
            next_index = min(visible_count - 1, index + 1) if action == "approve" else index
            location = "/?" + urlencode({"i": next_index, "msg": "已保存"})
            self.send_response(303); self.send_header("Location", location); self.end_headers()

        def log_message(self, format, *args):
            return
    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="启动本地岗位人工审核页面")
    parser.add_argument("--review-queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not args.review_queue.is_file():
        raise FileNotFoundError(f"请先生成审核队列：{args.review_queue}")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.review_queue))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"岗位审核页面已启动：{url}\n关闭窗口或按 Ctrl+C 停止。", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
