from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output" / "all_it_roles_knowledge_graph_v5_neo4j"
CONFIG_PATH = BASE_DIR / "config" / "neo4j_connection.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="将图谱构建结果同步到本地Neo4j")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--include-normalized", action="store_true")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    config = json.loads(args.config.resolve().read_text(encoding="utf-8"))
    stage = output_dir / "neo4j"
    import_dir = Path(config["import_dir"])
    import_dir.mkdir(parents=True, exist_ok=True)
    for source in stage.glob("*.csv"):
        shutil.copy2(source, import_dir / source.name)

    environment = os.environ.copy()
    environment["JAVA_HOME"] = config["java_home"]
    environment["PATH"] = f"{Path(config['java_home']) / 'bin'};{environment.get('PATH', '')}"
    common = [
        config["cypher_shell"], "-a", config["bolt_uri"], "-u", config["username"],
        "-p", config["password"],
    ]
    for script in (stage / "constraints.cypher", stage / "import.cypher"):
        subprocess.run([*common, "-f", str(script)], env=environment, check=True)
    if args.include_normalized:
        normalized = output_dir / "neo4j_normalized"
        for source in normalized.glob("*.csv"):
            shutil.copy2(source, import_dir / source.name)
        subprocess.run(
            [*common, "-f", str(normalized / "import_normalized.cypher")],
            env=environment,
            check=True,
        )
    print("Neo4j 同步完成")


if __name__ == "__main__":
    main()
