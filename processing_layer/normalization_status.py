from __future__ import annotations

import json
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORK_DIR = PROJECT_ROOT / "output" / "processed_normalization_full"
DATABASE = WORK_DIR / "knowledge_graph.db"
REPORT = WORK_DIR / "skill_reports" / "normalization_report.json"


def main() -> None:
    if not DATABASE.exists():
        print("status=NOT_STARTED")
        return
    connection = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    try:
        state = dict(connection.execute("SELECT key, value FROM export_state"))
    except sqlite3.OperationalError:
        state = {}
    finally:
        connection.close()
    report_matches_export = False
    if REPORT.exists():
        try:
            report_payload = json.loads(REPORT.read_text(encoding="utf-8"))
            report_matches_export = int(report_payload.get("input_mentions", -1)) == int(
                state.get("ability_count", "0")
            )
        except (OSError, ValueError, TypeError):
            report_matches_export = False
    payload = {
        "status": (
            "NORMALIZATION_COMPLETED"
            if state.get("completed") == "1" and report_matches_export
            else "EXPORT_COMPLETED"
            if state.get("completed") == "1"
            else "EXPORTING"
        ),
        "exported_jds": int(state.get("jd_count", "0")),
        "exported_abilities": int(state.get("ability_count", "0")),
        "checkpoint_cursor": state.get("cursor", ""),
        "report": str(REPORT) if REPORT.exists() else "",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
