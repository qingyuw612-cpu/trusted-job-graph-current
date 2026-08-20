"""统一启动岗位图谱、简历分析 API 与静态前端。"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
GRAPH_DIR = ROOT
RESUME_DIR = ROOT / "resume-analysis-agent"
FRONTEND_DIR = ROOT / "qianduan" / "html-main2"
RESUME_PYTHON = RESUME_DIR / ".venv" / "Scripts" / "python.exe"
DEFAULT_GRAPH_CONFIG = GRAPH_DIR / "config" / "neo4j_connection.json"
SERVICE_PORTS = {8000: "简历分析 API", 8010: "岗位图谱 API", 8090: "统一前端"}


def check_url(name: str, url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            ok = 200 <= response.status < 400
    except (OSError, urllib.error.URLError):
        ok = False
    print(f"[{'OK' if ok else '--'}] {name}: {url}")
    return ok


def port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def clean_process_env() -> dict[str, str]:
    """合并 Windows 中仅大小写不同的重复环境变量。"""
    merged: dict[str, tuple[str, str]] = {}
    for key, value in os.environ.items():
        normalized = key.upper()
        canonical = "Path" if normalized == "PATH" else key
        merged[normalized] = (canonical, value)
    return {key: value for key, value in merged.values()}


def validate_layout() -> bool:
    required = [
        GRAPH_DIR / "display_graph_handoff.py",
        RESUME_DIR / "api_server.py",
        FRONTEND_DIR / "index.html",
        FRONTEND_DIR / "resume-match.html",
    ]
    ok = True
    for path in required:
        exists = path.is_file()
        print(f"[{'OK' if exists else '!!'}] {path.relative_to(ROOT)}")
        ok = ok and exists
    python_ok = RESUME_PYTHON.is_file()
    print(f"[{'OK' if python_ok else '!!'}] {RESUME_PYTHON.relative_to(ROOT)}")
    return ok and python_ok


def service_check() -> bool:
    layout_ok = validate_layout()
    print()
    statuses = [
        check_url("岗位图谱 API", "http://127.0.0.1:8010/api/health"),
        check_url("简历分析 API", "http://127.0.0.1:8000/health"),
        check_url("统一前端", "http://127.0.0.1:8090/index.html"),
    ]
    return layout_ok and all(statuses)


def spawn(
    label: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    print(f"[启动] {label}: {' '.join(command)}")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        creationflags=creationflags,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查目录与服务状态")
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=Path(os.environ.get("NEO4J_CONFIG", DEFAULT_GRAPH_CONFIG)),
        help="Neo4j 连接 JSON；缺失时仍启动简历 API 与前端",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check:
        return 0 if service_check() else 1
    if not validate_layout():
        print("目录或虚拟环境不完整，请先查看 INTEGRATION.md。", file=sys.stderr)
        return 2

    occupied = [(port, label) for port, label in SERVICE_PORTS.items() if port_open(port)]
    if occupied:
        details = "、".join(f"{label}({port})" for port, label in occupied)
        print(
            f"检测到已有服务正在运行：{details}\n"
            "请直接打开 http://127.0.0.1:8090/index.html；"
            "如需应用新的环境变量，请先在原终端按 Ctrl+C 停止旧服务后再启动。",
            file=sys.stderr,
        )
        return 3

    processes: list[subprocess.Popen[str]] = []
    python = str(RESUME_PYTHON)
    config = args.neo4j_config.expanduser().resolve()
    try:
        resume_env = clean_process_env()
        graph_config: dict[str, object] = {}
        if config.is_file():
            graph_config = json.loads(config.read_text(encoding="utf-8"))
            if not port_open(7687):
                instance_dir = Path(str(graph_config.get("instance_dir") or ""))
                neo4j_command = instance_dir / "bin" / "neo4j.bat"
                if neo4j_command.is_file():
                    neo4j_env = clean_process_env()
                    java_home = str(graph_config.get("java_home") or "")
                    if java_home:
                        neo4j_env["JAVA_HOME"] = java_home
                    processes.append(
                        spawn(
                            "Neo4j 数据库",
                            [
                                os.environ.get("COMSPEC", "cmd.exe"),
                                "/d",
                                "/c",
                                f'"{neo4j_command}" console',
                            ],
                            instance_dir,
                            env=neo4j_env,
                        )
                    )
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline and not port_open(7687):
                        if processes[-1].poll() is not None:
                            break
                        time.sleep(1)
                    if not port_open(7687):
                        raise RuntimeError("Neo4j 未能在 90 秒内启动，请检查控制台日志")
                else:
                    raise RuntimeError(
                        f"Neo4j 未运行，且配置中的启动程序不存在: {neo4j_command}"
                    )
            resume_env.update(
                {
                    "STORE_BACKEND": "neo4j",
                    "NEO4J_URI": str(
                        graph_config.get("bolt_uri") or "bolt://127.0.0.1:7687"
                    ),
                    "NEO4J_USER": str(graph_config.get("username") or "neo4j"),
                    "NEO4J_PASSWORD": str(graph_config.get("password") or ""),
                    "NEO4J_DATABASE": str(graph_config.get("database") or "neo4j"),
                }
            )
        processes.append(
            spawn(
                "简历分析 API",
                [python, "api_server.py"],
                RESUME_DIR,
                env=resume_env,
            )
        )
        processes.append(
            spawn(
                "统一前端",
                [python, "-m", "http.server", "8090", "--bind", "127.0.0.1"],
                FRONTEND_DIR,
            )
        )
        if config.is_file():
            processes.append(
                spawn(
                    "岗位图谱 API",
                    [
                        python,
                        "display_graph_handoff.py",
                        "serve",
                        "--neo4j-config",
                        str(config),
                    ],
                    GRAPH_DIR,
                )
            )
        else:
            print(
                f"[跳过] 岗位图谱 API：缺少 {config}\n"
                "       复制 config/neo4j_connection.example.json 并填写后重启即可。"
            )

        print("\n统一入口: http://127.0.0.1:8090/index.html")
        print("按 Ctrl+C 停止全部服务。\n")
        time.sleep(1.0)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        failed = [process.returncode for process in processes if process.returncode]
        return failed[0] if failed else 0
    except KeyboardInterrupt:
        return 0
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
