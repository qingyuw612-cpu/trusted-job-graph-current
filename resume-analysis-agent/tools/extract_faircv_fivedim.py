# -*- coding: utf-8 -*-
"""规则提取 FairCV 简历 -> five_dim（与 test2 JSON 结构一致）。v3

用法:
    python tools/extract_faircv_fivedim.py [输入JSON] [输出目录]
默认输入 samples/faircv_sample_100.json，输出 samples/faircv_fivedim/。
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples" / "faircv_sample_100.json"
OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "samples" / "faircv_fivedim"

SKILL_HINTS = ["熟练", "精通", "掌握", "使用", "开发", "设计", "实现", "搭建", "调试", "部署", "编写", "分析", "优化", "构建"]

SCHOOLS = ["北京大学", "清华大学", "浙江大学", "复旦大学", "上海交通大学", "南京大学", "武汉大学",
           "华中科技大学", "西安交通大学", "北京理工大学", "电子科技大学", "哈尔滨工业大学",
           "麻省理工学院", "斯坦福", "卡内基梅隆", "香港大学", "中山大学", "东南大学", "同济大学"]

MOTIVATION_MAP = [
    ("热爱", "对技术有热情"), ("热情", "对技术有热情"), ("兴趣", "对技术有热情"),
    ("积极主动", "积极主动"), ("学习意愿", "学习意愿"), ("自我驱动", "自我驱动"),
    ("自驱", "自我驱动"), ("上进", "上进心"), ("探索", "探索精神"), ("求知", "求知欲"),
    ("持续学习", "持续学习"), ("成长", "成长意愿"),
]
TRAIT_MAP = [
    ("沟通能力", "沟通能力"), ("沟通协调", "沟通协调能力"), ("团队合作", "团队协作"),
    ("团队协作", "团队协作"), ("逻辑思维", "逻辑思维"), ("学习能力", "学习能力"),
    ("问题解决", "问题解决"), ("抗压", "抗压能力"), ("细心", "细心"), ("细致", "细致"),
    ("耐心", "耐心"), ("创新思维", "创新思维"), ("数据驱动", "数据驱动"), ("执行力", "执行力"),
    ("领导力", "领导力"), ("责任心", "责任心"), ("逻辑", "逻辑思维"), ("沟通", "沟通能力"),
    ("创新", "创新思维"), ("学习", "学习能力"),
]
SELF_CONCEPT_MAP = [
    ("责任心", "责任心"), ("团队合作意识", "团队合作意识"), ("团队合作", "团队合作意识"),
    ("主人翁", "主人翁意识"), ("领导力", "领导力"), ("职业素养", "职业素养"), ("敬业", "敬业精神"),
    ("担当", "担当意识"), ("奉献", "奉献精神"),
]


def extract_sections(content: str) -> dict:
    sections = {}
    current = "头部"
    for line in content.splitlines():
        stripped = line.strip()
        m = re.match(r"^(?:#+\s*|\*\*)([^*#\s][^*]*?)(?:\*\*)?\s*$", stripped)
        if m and len(m.group(1)) <= 20 and "：" not in m.group(1) and not m.group(1).startswith("{"):
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if stripped and not re.match(r"^[-*]\s*$", stripped):
            sections.setdefault(current, []).append(stripped)
    return sections


def parse_personal_info(section_lines: list) -> dict:
    info = {"姓名": "", "性别": "", "年龄/出生日期": "", "联系电话": "", "邮箱": "", "现居城市": "", "求职意向": ""}
    for line in section_lines:
        m = re.match(r"^-?\s*\**?(姓名|年龄|性别|婚姻状况|户口地|政治面貌|身体状况|邮箱|电话|现居城市|求职意向|期望职位)[：:]\s*(.*)", line)
        if m:
            key, val = m.group(1), m.group(2).strip().strip("*")
            if key == "姓名": info["姓名"] = val
            elif key == "年龄": info["年龄/出生日期"] = val
            elif key == "性别": info["性别"] = val
            elif key == "电话": info["联系电话"] = val
            elif key == "邮箱": info["邮箱"] = val
            elif key == "户口地": info["现居城市"] = val
            elif key in ("求职意向", "期望职位"): info["求职意向"] = val
    return info


def parse_edu_block(content: str) -> tuple:
    m = re.search(
        r"教育背景(.*?)(?:\n\s*(?:#{1,4}|\*\*)[^*#]{0,15}(?:专业技能|项目经验|工作经历|其他亮点|研究成果|产品经验|附加信息|个人简历))",
        content, re.S)
    block = m.group(1) if m else ""
    block = re.sub(r"[-*#`]", "", block)

    quals = []
    for pat, label in [(r"博士", "博士学历"), (r"硕士", "硕士学历"), (r"MBA", "MBA学历"), (r"本科|学士", "本科学历")]:
        if re.search(pat, block) and label not in quals:
            quals.append(label)
    # 专业: 排除学校名
    major_m = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,12}(?:科学与技术|工程|科学|技术|管理|设计|信息|学)\s*(?:专业)?)", block)
    if major_m:
        major = re.sub(r"\s+", "", major_m.group(1))
        if any(s in major for s in SCHOOLS) and not major.endswith("专业"):
            major = None
        if major and not major.endswith("专业"):
            major += "专业"
        if major and major not in quals:
            quals.append(major)

    knowledge = []
    for cm in re.findall(r"(?:相关课程|专业课程|主要课程|课程)[：:]\s*([^\n]*)", block):
        for part in re.split(r"[、,，;；/]", cm):
            part = part.strip()
            if part and len(part) <= 40 and not re.match(r"^[-*0-9.]+$", part):
                knowledge.append(part)
    for cl in re.findall(r"^\s*[-•*]\s*([^\n]{2,40})$", block, re.M):
        cl = cl.strip()
        if re.search(r"算法|系统|原理|网络|数据库|编程|结构|开发|设计|课程", cl) and cl not in knowledge and len(cl) <= 40:
            knowledge.append(cl)
    knowledge = list(dict.fromkeys(knowledge))[:12]
    return list(dict.fromkeys(quals)), knowledge


def extract_skills(content: str) -> list:
    sections = extract_sections(content)
    skill = []
    for sec in ("专业技能", "项目经验", "工作经历", "产品经验", "其他亮点"):
        for line in sections.get(sec, []):
            s = line.strip().lstrip("-*• ").strip()
            if not s or len(s) < 2:
                continue
            s = re.sub(r"^[^：:]{1,12}[：:]\s*", "", s)
            s = re.sub(r"[（(].*?[)）]", "", s).strip()
            if not s:
                continue
            if not (any(h in s for h in SKILL_HINTS) or re.search(r"[A-Za-z]", s)):
                continue
            if re.search(r"项目|成果|时间|GPA|排名|获奖|荣誉|职责|角色|贡献|规模|选型$", s):
                continue
            parts = re.split(r"[、,，;；/]", s)
            for p in parts:
                p = p.strip().strip("*-").strip()
                if p and len(p) <= 40 and not re.match(r"^[-*0-9.]+$", p) and p not in skill:
                    skill.append(p)
    return list(dict.fromkeys(skill))[:20]


def _match_keywords(text: str, mapping: list) -> list:
    out = []
    for kw, label in mapping:
        if kw in text and label not in out:
            out.append(label)
    return out


def extract_five_dim(resume: dict) -> dict:
    content = resume["content"]
    meta = resume["metadata"]
    sections = extract_sections(content)

    personal_info = parse_personal_info(sections.get("个人信息", []) + sections.get("个人简历", []))
    if not personal_info["求职意向"]:
        personal_info["求职意向"] = meta.get("position", "")

    qualifications, knowledge = parse_edu_block(content)
    skill = extract_skills(content)

    free_text = "\n".join(sections.get("自我评价", []) + sections.get("其他亮点", []) + sections.get("工作期望", []))
    motivation = _match_keywords(free_text, MOTIVATION_MAP)
    trait = _match_keywords(free_text, TRAIT_MAP)
    self_concept = _match_keywords(free_text, SELF_CONCEPT_MAP)
    if not trait:
        for kw in ["沟通", "协作", "逻辑", "学习", "抗压", "细致", "创新", "数据"]:
            if kw in free_text:
                trait.append(kw + "能力")
                break

    return {
        "personal_info": personal_info,
        "knowledge": knowledge,
        "skill": skill,
        "qualifications": qualifications,
        "motivation": motivation,
        "trait": trait,
        "self_concept": self_concept,
    }


def main():
    with open(INPUT, encoding="utf-8") as f:
        resumes = json.load(f)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"knowledge": 0, "skill": 0, "qualifications": 0, "motivation": 0, "trait": 0, "self_concept": 0}
    for i, resume in enumerate(resumes):
        meta = resume["metadata"]
        five_dim = extract_five_dim(resume)
        for k in stats:
            if five_dim[k]:
                stats[k] += 1
        out = {
            "file_name": f"faircv_{i:03d}_{meta['position']}.json",
            "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "raw_text": resume["content"],
            "is_markdown": True,
            "five_dim": five_dim,
        }
        with open(OUT_DIR / f"{i:03d}_{meta['position']}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"提取完成: {len(resumes)} 份 -> {OUT_DIR}")
    print("非空维度覆盖:", {k: f"{v}/{len(resumes)}" for k, v in stats.items()})
    sample = extract_five_dim(resumes[0])
    for k, v in sample.items():
        if isinstance(v, list):
            print(f"  {k}: {len(v)} 条 | {json.dumps(v[:6], ensure_ascii=False)[:160]}")
        else:
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:160]}")


if __name__ == "__main__":
    main()
