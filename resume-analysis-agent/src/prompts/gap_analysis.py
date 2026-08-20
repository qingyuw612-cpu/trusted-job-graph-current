"""提示词模板 — 7 维差距分析（ROLE_GAP_PROMPT，基于结构化命中清单）。

核心口径为 7 维：knowledge / skill / qualifications / preference /
motivation / trait / self_concept。
"""


# ==================== Role gap analysis (streamlined: pre-computed hits) ====================

ROLE_GAP_PROMPT = """You are a career advisor. Below is the skill match between a candidate and a target role.

## Target Role
{role_name} (family: {family_name} / domain: {domain_name})

## Pre-computed Match Details
{dimension_details}

## Missing Skills (pre-sorted by support weight, high first)
{missing_sorted}

## Resume (context only)
{resume_raw_text}

## Task
1. Judge match level for each dimension (missing / partial / sufficient).
2. Output the missing skills in the SAME order as the pre-sorted list above,
   filling importance (high/medium/low) based on weight and your judgment.
3. Generate a learning path (max 8 steps) for the missing skills. Order the steps
   by combining importance (higher first) with prerequisite/knowledge dependency
   (must-learn-first); the final order may differ from the input list when
   prerequisites require it.
   Each step must include: skill, importance, prerequisite, resources,
   estimated_effort, why.
4. Output overall advice.

## Output (JSON only)
{{
  "match": {{"verdict": "yes|no", "reason": "one sentence"}},
  "dimensions": {{
    "knowledge":      {{"gap_level": "missing|partial|sufficient", "summary": ""}},
    "skill":          {{"gap_level": "missing|partial|sufficient", "summary": ""}},
    "qualifications": {{"gap_level": "missing|partial|sufficient", "summary": ""}},
    "preference":     {{"gap_level": "missing|partial|sufficient", "summary": ""}},
    "motivation":     {{"gap_level": "missing|partial|sufficient", "summary": ""}},
    "trait":          {{"gap_level": "missing|partial|sufficient", "summary": ""}},
    "self_concept":   {{"gap_level": "missing|partial|sufficient", "summary": ""}}
  }},
  "missing_skills": [
    {{"skill": "skill name", "dim": "dimension key", "importance": "high|medium|low"}}
  ],
  "overall_summary": "",
  "learning_path": [
    {{
      "step": 1,
      "skill": "skill/topic",
      "importance": "high|medium",
      "prerequisite": "prerequisite or 无",
      "resources": ["resource 1", "resource 2"],
      "estimated_effort": "e.g. 2-3 weeks",
      "why": "why this step comes first"
    }}
  ]
}}
"""

