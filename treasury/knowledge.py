"""知识时点（as_of）过滤：任何一行数据在某个时点是否"已知"。

纪律：
- 应收款以 signed（签约日）进入财务视野，received 为空表示截至资料批次无回执；
- 已收现金只有 received <= as_of 才能计入当时账上；
- 集团关系的 known 晚于 as_of 时不可见（见 relations.py）；
- 决议以 approved 进入"已承诺"，paid 才表示已划款；
- 义务来自赛季开始时的预算快照，赛季内各 as_of 均可见整季预算义务。
"""
from __future__ import annotations

from dataclasses import dataclass

from .calendar import d

# 已收现金 / 可撤销款项 / 承诺未到期 / 过期未付
RECEIVED = "received"
REVOCABLE = "revocable"
OVERDUE_UNPAID = "overdue_unpaid"
COMMITTED_NOT_DUE = "committed_not_due"


@dataclass(frozen=True)
class ReceivableView:
    row: dict
    signed: object
    due: object
    known: bool
    received: bool
    overdue: bool
    revocable: bool
    state: str


def _view(row: dict, as_of) -> ReceivableView:
    as_of = d(as_of)
    signed = d(row["signed"])
    due = d(row["due"])
    known = signed <= as_of
    received_date = row["received"]
    received = received_date is not None and d(received_date) <= as_of
    unpaid = not received
    overdue = unpaid and due < as_of
    revocable = unpaid and bool(row["cancellable"])
    if received:
        state = RECEIVED
    elif revocable:
        state = REVOCABLE
    elif overdue:
        state = OVERDUE_UNPAID
    else:
        state = COMMITTED_NOT_DUE
    return ReceivableView(row, signed, due, known, received, overdue, revocable, state)


def visible_receivables(rows: list[dict], as_of) -> list[ReceivableView]:
    """截至 as_of 已签约的应收款视图（含尚未回款者，按状态分类）。"""
    return [v for v in (_view(r, as_of) for r in rows) if v.known]


def received_receivables(rows: list[dict], as_of) -> list[dict]:
    """as_of 时已拿到回执的现金，按回执日入账。"""
    return [v.row for v in visible_receivables(rows, as_of) if v.received]


def known_resolutions(rows: list[dict], as_of) -> list[dict]:
    """as_of 前已通过的决议；paid 与 confirmed 都构成不可再部署的承诺。"""
    as_of = d(as_of)
    return [r for r in rows if d(r["approved"]) <= as_of]


def assert_no_lookahead(views: list[ReceivableView], as_of) -> None:
    """回测守卫：as_of 之后才有的信息不得进入当时判断。

    合法情形：as_of 前签约、as_of 后才回执的合同属于"已知但未回款"，
    其 received 标记必须为 False（由 _view 保证）；这里再硬性复核一次，
    并拦截任何晚于 as_of 的签约。
    """
    as_of = d(as_of)
    for v in views:
        row = v.row
        if d(row["signed"]) > as_of:
            raise AssertionError(f"前视泄漏：{row['id']} 的签约晚于 as_of")
        if row["received"] is not None and d(row["received"]) > as_of and v.received:
            raise AssertionError(
                f"前视泄漏：{row['id']} 的晚到回执 {row['received']} 被计为 as_of {as_of} 的已收现金"
            )
