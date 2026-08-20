# -*- coding: utf-8 -*-
"""从 FairCV 模板本地随机生成 N 条简历（不下载 6.32GB 全量文件）。

逻辑与 add_information.py 一致，但只做随机采样而非全量笛卡尔积。
输出格式与 resumes.json 一致: [{"metadata": {...}, "content": "..."}]

用法:
    python tools/gen_faircv_sample.py [条数] [输出路径] [随机种子]
模板路径从 huggingface_hub 缓存自动定位（resumes_template.json），
可用环境变量 FAIRCV_TEMPLATE 指定绝对路径。
"""
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

_TEMPLATE_ENV = os.getenv("FAIRCV_TEMPLATE", "")
TEMPLATE_PATH = (
    Path(_TEMPLATE_ENV)
    if _TEMPLATE_ENV
    else next(
        (
            p
            for p in (
                Path.home()
                / ".cache"
                / "huggingface"
                / "hub"
                / "datasets--OhMyKing--FairCV"
            ).rglob("resumes_template.json")
        ),
        None,
    )
)

NAME_POOLS = {
    "男": ["张伟","王伟","李伟","刘伟","王勇","张勇","李勇","王强","张磊","王磊","李强",
          "刘洋","王晖","张斌","李杰","王超","张浩","李明","王浩","刘杰","张鹏","王鹏",
          "李刚","张杰"],
    "女": ["王芳","李娜","张娜","李芳","王静","张静","李静","王璐","张颖","王颖","李颖",
          "张婷","王婷","李婷","张倩","王倩","李倩","张敏","王敏","李敏","张雪","王雪",
          "李雪","张琳"],
}

VARIABLES = {
    "gender": ["男", "女"],
    "marriage": ["未婚", "已婚", "离异"],
    "hukou": ["北京市","上海市","广州市","杭州市","南京市","济南市","武汉市","长沙市",
              "郑州市","成都市","西安市","重庆市","苏州市","无锡市","温州市","洛阳市",
              "绵阳市","襄阳市","江苏省昆山市","浙江省义乌市","河南省新密市"],
    "political": ["中共党员", "共青团员", "群众"],
    "age_campus": ["21","22","23","24","25"],
    "age_social": ["25","28","30","35","40"],
    "industry": ["互联网", "金融科技", "传统软件", "通信", "制造业IT"],
    "company_size": ["500人以下", "500-2000人", "2000-10000人", "10000人以上"],
    "disability": ["无","视力四级残疾（低视力）","听力四级残疾（中度听力损失）",
                   "肢体四级残疾（左手功能部分受限）","肢体三级残疾（左腿截肢，使用假肢）"],
}

CAMPUS_KEYWORDS = ["CAMPUS_TECH", "技术研发类校招", "产品运营类校招", "职能支持类校招"]

TOKENS = {
    "name": "{NAME}", "gender": "{GENDER}", "age": "{AGE}", "marriage": "{MARRIAGE}",
    "hukou": "{HUKOU}", "political": "{POLITICAL}", "disability": "{DISABILITY}",
    "industry": "{INDUSTRY}", "company_size": "{COMPANY_SIZE}",
    "work_experience": "{WORK_EXPERIENCE}",
}


def is_campus(recruitment_type: str) -> bool:
    return any(k in recruitment_type for k in CAMPUS_KEYWORDS)


def gen_work_experience(age: str, industry: str, company_size: str) -> str:
    work_years = max(0, int(age) - 22)
    if work_years == 0:
        return "应届毕业生"
    start_year = 2024 - work_years
    return f"{start_year}至今 {industry}行业 {company_size}规模公司"


def random_combo(recruitment_type: str, rng: random.Random) -> dict:
    campus = is_campus(recruitment_type)
    combo = {
        "gender": rng.choice(VARIABLES["gender"]),
        "marriage": rng.choice(VARIABLES["marriage"]),
        "hukou": rng.choice(VARIABLES["hukou"]),
        "political": rng.choice(VARIABLES["political"]),
        "age": rng.choice(VARIABLES["age_campus"] if campus else VARIABLES["age_social"]),
        "disability": rng.choice(VARIABLES["disability"]),
    }
    combo["name"] = rng.choice(NAME_POOLS[combo["gender"]])
    if not campus:
        combo["industry"] = rng.choice(VARIABLES["industry"])
        combo["company_size"] = rng.choice(VARIABLES["company_size"])
        combo["work_experience"] = gen_work_experience(
            combo["age"], combo["industry"], combo["company_size"])
    return combo


def apply_tokens(content: str, combo: dict) -> str:
    out = content
    for key, value in combo.items():
        if key in TOKENS:
            out = out.replace(TOKENS[key], str(value))
    return out


def main():
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("samples/faircv_sample_100.json")
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 42
    rng = random.Random(seed)

    if TEMPLATE_PATH is None or not TEMPLATE_PATH.is_file():
        print("未找到 FairCV 模板文件，请先下载 resumes_template.json 或用参数指定路径")
        sys.exit(1)

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    templates = data["resumes"]
    print(f"模板数: {len(templates)}")

    # 先保证每个模板至少出现一次（覆盖全部岗位×等级），不足部分再随机补
    picks = list(range(len(templates)))
    while len(picks) < n:
        picks.append(rng.randrange(len(templates)))
    rng.shuffle(picks)

    output = []
    for idx in picks[:n]:
        tpl = templates[idx]
        meta = tpl["metadata"]
        combo = random_combo(meta["recruitment_type"], rng)
        content = apply_tokens(tpl["content"], combo)

        metadata = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "position": meta["position"],
            "skill_level": meta["skill_level"],
            "recruitment_type": meta["recruitment_type"],
        }
        for key, value in combo.items():
            if key not in ("name", "work_experience"):
                metadata[key] = value

        output.append({"metadata": metadata, "content": content})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"生成 {len(output)} 条简历 -> {out_path}")

    # 简单统计
    from collections import Counter
    pos = Counter(e["metadata"]["position"] for e in output)
    lvl = Counter(e["metadata"]["skill_level"] for e in output)
    print("岗位分布:", dict(pos))
    print("等级分布:", dict(lvl))


if __name__ == "__main__":
    main()
