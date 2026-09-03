# -*- coding: utf-8 -*-
"""下载 FairCV 全量 resumes.json（6.32GB，支持断点续传）。

用法:
    python tools/download_faircv.py [目标目录]

下载到 HF 缓存后复制到目标目录（默认当前目录下 FairCV/）。
"""
import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from huggingface_hub import hf_hub_download

DEST_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "FairCV"


def main():
    print("开始下载 data/resumes.json (6.32GB) ...")
    path = hf_hub_download(
        "OhMyKing/FairCV",
        "data/resumes.json",
        repo_type="dataset",
    )
    print("下载完成:", path)

    if DEST_DIR.is_dir():
        dest = DEST_DIR / "resumes.json"
        print(f"复制到 {dest} ...")
        shutil.copy2(path, dest)
        print("复制完成:", dest, f"({dest.stat().st_size/1e9:.2f}GB)")
    else:
        print(f"目标目录不存在，跳过复制: {DEST_DIR}")


if __name__ == "__main__":
    main()
