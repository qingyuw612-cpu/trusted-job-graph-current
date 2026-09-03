"""提示词模板 — LLM 语义复核（enhance_matches）。"""

ENHANCE_PROMPT = """You are a meticulous resume-JD matcher. A keyword-based matcher has produced a ranked list of roles for the candidate. Your job is to review the hit/miss lists for the Top-{topk} roles and fix any false positives/negatives using semantic understanding.

## Resume (context only)
{resume_text}

## Keyword Match Results (JSON)
{rank_json}

## Task
For each role in the list:
1. Review every dimension's "hit" and "miss" skill names.
2. A skill is a REAL hit only if the resume genuinely demonstrates it (synonyms, project context, or explicit mention count). Remove hits that are pure keyword coincidences.
3. A skill is a MISSED hit only if the resume clearly demonstrates it but the keyword matcher missed it. Only add skills that appear in the keyword results' "miss" list.
4. Do NOT invent skills not present in the original match data.
5. Recompute "score" proportionally to the corrected hit_count/total_skills ratio (keep 4 decimals).
6. Add "review_note" to each role summarizing corrections made (or "无修正").

## Output (JSON only, no other text)
{{
  "topk": {topk},
  "results": [
    {{
      "role_name": "...",
      "score": 0.0000,
      "hit_skills": N,
      "total_skills": M,
      "review_note": "...",
      "dimensions": {{
        "knowledge": {{"hit": [...], "miss": [...]}},
        "skill": {{"hit": [...], "miss": [...]}},
        "qualifications": {{"hit": [...], "miss": [...]}},
        "preference": {{"hit": [...], "miss": [...]}},
        "motivation": {{"hit": [...], "miss": [...]}},
        "trait": {{"hit": [...], "miss": [...]}},
        "self_concept": {{"hit": [...], "miss": [...]}}
      }}
    }}
  ]
}}
"""

