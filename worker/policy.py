"""Source-backed audience classification and publication policy.

Audience scope is independent of the legacy population_share/conditional_share
indicator type: a bank's customers can be targetable while still a subgroup.
"""
import json
import re
from pathlib import Path


def normalized(value):
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def verbatim(value, text):
    return bool(value and normalized(value) in normalized(text))


def population_scope(finding, text, approved_ids=None):
    """Return base, conditional or unknown without inventing denominator facts."""
    if approved_ids is None:
        registry = json.loads(Path(__file__).with_name("targeting_criteria.json").read_text(encoding="utf-8"))
        approved_ids = {item["id"] for item in registry["criteria"]}
    else:
        approved_ids = set(approved_ids)
    base = finding.get("percent_base")
    if not verbatim(base, text):
        return "unknown"
    requested = set(finding.get("targeting_criteria") or [])
    if finding.get("candidate_criteria") or requested - approved_ids:
        return "conditional"
    methodology = finding.get("methodology_evidence")
    if not verbatim(methodology, text):
        methodology = ""
    support = normalized(f"{base} {methodology}")
    denominator = support
    if re.search(r"молодеж", normalized(f"{base} {finding.get('audience', '')}")):
        # An age elsewhere in the article (e.g. all respondents 18+) does not
        # establish the boundaries of the youth cohort.
        defined_youth = any(re.search(
            r"молодеж\w*[^.;!?]{0,65}?(?:от\s*)?\d{1,2}\s*(?:[-–—]|до|по)\s*\d{1,2}\s*(?:лет|год)",
            fragment,
        ) for fragment in (normalized(base), normalized(methodology)))
        if not defined_youth:
            return "unknown"
    if re.search(r"покупател|продавц|пациент|страдающ|болевш|использующ.{0,25}(?:ии|нейросет)", denominator):
        return "conditional"
    if re.search(r"\b(?:читающ|покупающ|продающ|предпочитающ|планирующ|довольн|недовольн|посещающ|смотрящ|любящ|намеревающ)\w*", denominator):
        return "conditional"
    if re.search(r"пользовател|клиент|использующ|пользующ", denominator):
        banking = "banking_product_use" in requested and re.search(
            r"банк|карт|кредит|вклад|депозит|счет|ипотек|заемщик", denominator
        )
        if not banking:
            return "conditional"
    requirements = [
        (r"родител|матер|мам|отц|сем[ьи].{0,15}дет", {"children", "children_age"}),
        (r"руководител|директор|менеджер|специалист", {"job_title"}),
        (r"сотрудник|работник|работающ", {"employment_status", "job_title", "department", "company_industry", "business_size"}),
        (r"автовладел|владельц.{0,15}автомоб", {"car_ownership"}),
        (r"владельц.{0,15}кош", {"cat_ownership"}),
        (r"владельц.{0,15}собак", {"dog_ownership"}),
    ]
    for pattern, ids in requirements:
        if re.search(pattern, denominator) and not requested.intersection(ids):
            return "conditional"
    if finding.get("base_kind") == "targetable_inferred" and (
        not methodology or not finding.get("base_reason")
    ):
        return "unknown"
    if requested:
        if finding.get("base_kind") in {"targetable_explicit", "targetable_inferred"} or finding.get("population_scope") == "base":
            return "base"
        # Source-proven bank/parent/professional audiences stay targetable even
        # when the legacy indicator type or base_kind describes a subgroup.
        if any(re.search(pattern, denominator) and requested.intersection(ids) for pattern, ids in requirements):
            return "base"
        if "banking_product_use" in requested and re.search(r"банк|карт|кредит|вклад|счет|ипотек|заемщик", denominator):
            return "base"
    if not requested and finding.get("base_kind") in {"conditional", "unknown"}:
        return "conditional" if finding.get("base_kind") == "conditional" else "unknown"
    if re.search(r"населени|россиян|респондент|жител|граждан|опрошенн|мужчин|женщин|молодеж", denominator):
        return "base"
    return "unknown"


def select_primary_findings(findings, limit=5):
    """Keep every validated row, publishing only the model's eligible choices.

The limit covers the whole material, not each block. Never promote unchosen
rows to fill a quota, and never publish a pure sample composition statistic.
"""
    rows = [dict(row) for row in findings]
    limit = max(0, min(5, int(limit)))
    eligible = [i for i, row in enumerate(rows)
                if row.get("is_demographic_breakdown") is not True
                and row.get("population_scope") in {"base", "conditional"}]
    preferred = "base" if any(rows[i].get("population_scope") == "base" for i in eligible) else "conditional"
    choices = [i for i in eligible if rows[i].get("is_primary") is True
               and rows[i].get("population_scope") == preferred
               and isinstance(rows[i].get("primary_reason"), str)
               and rows[i]["primary_reason"].strip()]
    def ranking(index):
        rank = rows[index].get("primary_rank")
        return (rank if type(rank) is int and rank > 0 else 10**6, index)
    chosen = sorted(choices, key=ranking)[:limit]
    ranks = {index: rank for rank, index in enumerate(chosen, 1)}
    for i, row in enumerate(rows):
        row["is_primary"] = i in ranks
        row["primary_rank"] = ranks.get(i)
    return rows
