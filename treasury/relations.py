"""付款方集团关系的时点解析。

payer_relations 里：
- 普通行给出 sponsor -> payer_group，effective 为真实生效日，known 为财务登记日；
- supersedes 行是对旧关系的更正（如 L3-R 更正 L3），同样有 effective/known；
- payer_group 为空表示财务尚未确认集团归属（如 S6）。

知识时点 as_of 的纪律：更正只有在 known <= as_of 时才能用于当时的判断，
但它自 effective 起对业务生效；因此同一份资料在不同 as_of 下会得到不同关系图，
这正是理事会需要看到的"结论对付款关联判断的敏感性"。
"""
from __future__ import annotations

from dataclasses import dataclass

from .calendar import d


@dataclass(frozen=True)
class Relation:
    sponsor: str
    payer_group: str | None
    effective: object
    known: object
    id: str


def active_relations(rows: list[dict], as_of) -> dict[str, Relation]:
    """返回 as_of 时点财务"已知且已生效"的 sponsor -> 关系映射。

    规则：
    1. known > as_of 的登记（含更正）一律不可见；
    2. 更正行 supersedes 旧行：可见时替换旧关系，不可见时沿用旧关系；
    3. effective > as_of 的关系尚未生效，不返回。
    """
    as_of = d(as_of)
    superseded: set[str] = set()
    chosen: dict[str, Relation] = {}
    for row in rows:
        known = d(row["known"])
        effective = d(row["effective"])
        if known > as_of or effective > as_of:
            continue
        sponsor = row["sponsor"]
        if row.get("supersedes"):
            superseded.add(row["supersedes"])
        chosen[sponsor] = Relation(
            sponsor=sponsor,
            payer_group=row["payer_group"],
            effective=effective,
            known=known,
            id=row["id"],
        )
    # 被可见更正替换掉的旧登记移除
    result: dict[str, Relation] = {}
    for sponsor, rel in chosen.items():
        if rel.id in superseded:
            continue
        result[sponsor] = rel
    return result


def group_of(relations: dict[str, Relation], sponsor: str) -> str | None:
    rel = relations.get(sponsor)
    return rel.payer_group if rel else None


def sponsors_in_groups(relations: dict[str, Relation], groups: list[str]) -> set[str]:
    wanted = set(groups)
    return {s for s, rel in relations.items() if rel.payer_group in wanted}


def unknown_group_sponsors(relations: dict[str, Relation]) -> set[str]:
    """已登记但集团归属为空的赞助方——关系未知，影响范围必须保留。"""
    return {s for s, rel in relations.items() if rel.payer_group is None}


def relation_corrections(rows: list[dict]) -> list[dict]:
    """全部更正登记，供敏感性分析使用（不做 as_of 过滤）。"""
    return [row for row in rows if row.get("supersedes")]
