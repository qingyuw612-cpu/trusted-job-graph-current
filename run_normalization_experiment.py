from __future__ import annotations

import argparse
import json
from pathlib import Path

from trusted_graph_agent.normalization_experiment import (
    NormalizationConfig,
    NormalizationExperiment,
    SentenceTransformerEmbedder,
)


BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="岗位技能向量标准化试验")
    parser.add_argument(
        "--database",
        type=Path,
        default=BASE_DIR / "output" / "normalization_pilot_raw" / "knowledge_graph.db",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=BASE_DIR / "output" / "normalization_pilot_result",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=BASE_DIR / "trusted_graph_agent" / "normalization_config.json",
    )
    parser.add_argument("--model", default="", help="本地模型目录或Hugging Face模型名")
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    parser.add_argument("--analyze-only", action="store_true")
    args = parser.parse_args()

    config = NormalizationConfig.load(args.config)
    if args.analyze_only:
        report = NormalizationExperiment(
            args.database.resolve(),
            args.output_dir.resolve(),
            config,
            None,
        ).analyze_candidate_pool()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    model_name = args.model or config.model_name
    embedder = SentenceTransformerEmbedder(model_name, config.embedding_batch_size, args.device)
    report = NormalizationExperiment(
        args.database.resolve(),
        args.output_dir.resolve(),
        config,
        embedder,
    ).run()
    print(json.dumps({key: value for key, value in report.items() if key != "top_skills"}, ensure_ascii=False, indent=2))
    print(f"\n详细报告：{args.output_dir.resolve() / 'normalization_report.md'}")


if __name__ == "__main__":
    main()
