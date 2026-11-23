"""压力情景：同一付款集团内的账期延迟一起发生。

stress.jsonl 每行一个情景：
- groups：受冲击的付款集团（集团内所有 sponsor 的应收一起延迟）；
- delay_days：账期在原定 due 之上顺延的营业日数；
- unlinked：集团归属未知的 sponsor，作为"可能同组"的敞口保留，
  报告必须同时给出纳入与不纳入该 sponsor 两种影响范围。

历史样本只用于估计"正常账期"：取每个 sponsor 历史上 received-due 的中位数，
回测时只使用 as_of 之前已经发生的样本，不能用后来的回执假装当时更准。
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .calendar import d, days_after
from .knowledge import ReceivableView
from .relations import Relation, sponsors_in_groups, unknown_group_sponsors

# 无历史样本时的兜底正常账期（资料中正常回款普遍为 due 后 9 天）。
FALLBACK_LAG_DAYS = 9


@dataclass(frozen=True)
class Scenario:
    id: str
    groups: tuple[str, ...]
    delay_days: int
    unlinked: tuple[str, ...]


def build_scenarios(stress_rows: list[dict]) -> list[Scenario]:
    out = []
    for row in stress_rows:
        out.append(
            Scenario(
                id=row["id"],
                groups=tuple(row["groups"]),
                delay_days=row["delay_days"],
                unlinked=tuple(row.get("unlinked", [])),
            )
        )
    return out


def normal_lag_by_sponsor(receivable_rows: list[dict], as_of) -> dict[str, int]:
    """用 as_of 前已回执的历史样本估计各 sponsor 的正常账期（中位数，向上取整）。"""
    as_of = d(as_of)
    samples: dict[str, list[int]] = {}
    for row in receivable_rows:
        if row["received"] is None:
            continue
        received = d(row["received"])
        if received > as_of:
            # 回测纪律：as_of 之后才有的回执不进样本
            continue
        lag = (received - d(row["due"])).days
        if lag < 0:
            continue
        samples.setdefault(row["sponsor"], []).append(lag)
    lags = {sponsor: int(median(vals)) for sponsor, vals in samples.items()}
    all_vals = [v for vals in samples.values() for v in vals]
    lags["__global__"] = int(median(all_vals)) if all_vals else FALLBACK_LAG_DAYS
    return lags


def affected_sponsors(
    scenario: Scenario, relations: dict[str, Relation]
) -> tuple[set[str], set[str]]:
    """返回 (确定受影响 sponsor, 可能受影响的未知 sponsor)。"""
    certain = sponsors_in_groups(relations, list(scenario.groups))
    unknown = unknown_group_sponsors(relations)
    possible = unknown.intersection(set(scenario.unlinked)) if scenario.unlinked else set()
    return certain, possible


def projected_receipt_date(
    view: ReceivableView,
    scenario: Scenario | None,
    relations: dict[str, Relation],
    normal_lags: dict[str, int],
    *,
    include_possible: bool = False,
):
    """一笔未回款应收在某情景下的预计到账日；已回款返回真实回执日；不在范围返回 None。

    None 表示该情景假设下这笔钱在压力中被撤销/不可依赖（可撤销款项）。
    """
    row = view.row
    if view.received:
        return d(row["received"])
    sponsor = row["sponsor"]
    certain, possible = (
        affected_sponsors(scenario, relations) if scenario is not None else (set(), set())
    )
    if scenario is None:
        lag = normal_lags.get(sponsor, normal_lags["__global__"])
        return days_after(row["due"], lag)
    if sponsor in certain or (include_possible and sponsor in possible):
        if row["cancellable"]:
            return None
        return days_after(row["due"], scenario.delay_days)
    # 不在受冲击集团：按正常账期
    lag = normal_lags.get(sponsor, normal_lags["__global__"])
    return days_after(row["due"], lag)
