"""俱乐部现金月历引擎：按日推进两个资金桶，判定每笔到期义务能否按时兑付。

资金桶（金额均为整数百元）：
- free：期初现金 + 普通赞助回款，可用于任何用途；
- earmarked：期初设备专款 + earmark=equipment 的回款，只能用于 equipment 义务。

事件顺序（同一营业日）：先入账（回款/保障金注入），再按 rank 顺序处理到期义务。
专款义务先用专款桶，不足部分才允许占用普通桶；普通桶不得为负，
无法按时全额支付的义务登记为 unpaid（无保证的现金断点），并继续推进，
以便看到后续回款能否在观察窗内补上（区分"短期跨月能力"与"真正断点"）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .calendar import d, month_key

PAYROLL = "payroll"
ENTRY = "entry"
EQUIPMENT = "equipment"
PURPOSES = (PAYROLL, ENTRY, EQUIPMENT)


@dataclass
class Receipt:
    date: object
    amount: int
    bucket: str  # "free" | "earmarked"
    source_id: str
    note: str = ""


@dataclass
class Injection:
    """保障金注入（来自 fund 引擎的拨付决定）。purpose 限定可用用途。"""

    date: object
    amount: int
    purpose: str
    source_id: str


@dataclass
class Repayment:
    """受益方回款后向保障金池的归还：从普通桶实际划出，保证两侧守恒。"""

    date: object
    amount: int
    source_id: str
    settled: int = 0  # 实际划出额（普通桶不足时小于 amount）


@dataclass
class ObligationOutcome:
    obligation_id: str
    club: str
    purpose: str
    rank: int
    due: object
    amount: int
    paid_on_time: int
    shortfall: int
    bucket_used: dict
    recovered_date: object | None  # 断点后若在观察窗内补齐，记录补齐日


@dataclass
class MonthState:
    month: str
    free_end: int
    earmarked_end: int
    due_total: int
    paid_on_time_total: int
    shortfall_total: int
    status: str  # "ok" | "bridge" | "break"


@dataclass
class ClubLedger:
    club: str
    horizon_end: object
    free: int
    earmarked: int
    receipts: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    months: list = field(default_factory=list)
    timeline: list = field(default_factory=list)

    def month_summary(self) -> list[MonthState]:
        return self.months


def _bucket_for_receivable(row: dict) -> str:
    return "earmarked" if row.get("earmark") == EQUIPMENT else "free"


def simulate_club(
    club_row: dict,
    obligation_rows: list[dict],
    receipts: list[Receipt],
    horizon_end,
    injections: list[Injection] | None = None,
    repayments: list[Repayment] | None = None,
) -> ClubLedger:
    """对单家俱乐部跑现金日历。obligation_rows 应已按该俱乐部与观察窗过滤。

    repayments 为向保障金池的归还，从普通桶划出；若普通桶不足则只划可用部分
    （不足部分保留为俱乐部对保障金池的欠款，由 fund 引擎另行登记）。
    """
    horizon_end = d(horizon_end)
    injections = injections or []
    repayments = repayments or []
    ledger = ClubLedger(
        club=club_row["id"],
        horizon_end=horizon_end,
        free=int(club_row["start_cash"]),
        earmarked=int(club_row["earmarked"]),
    )

    dated_events: dict = {}
    for rc in receipts:
        dated_events.setdefault(d(rc.date), []).append(("receipt", rc))
    for inj in injections:
        if d(inj.date) <= horizon_end:
            dated_events.setdefault(d(inj.date), []).append(("injection", inj))
    for rp in repayments:
        if d(rp.date) <= horizon_end:
            dated_events.setdefault(d(rp.date), []).append(("repayment", rp))
    for ob in sorted(obligation_rows, key=lambda x: (d(x["due"]), x["rank"], x["id"])):
        if d(ob["due"]) <= horizon_end:
            dated_events.setdefault(d(ob["due"]), []).append(("obligation", ob))

    pending: list[ObligationOutcome] = []

    for day in sorted(dated_events):
        for kind, payload in sorted(
            dated_events[day], key=lambda e: 0 if e[0] != "obligation" else 1
        ):
            if kind == "receipt":
                if payload.bucket == "earmarked":
                    ledger.earmarked += payload.amount
                else:
                    ledger.free += payload.amount
                ledger.receipts.append(payload)
            elif kind == "injection":
                # 按用途拨付：equipment 注入专款桶，其余进普通桶
                if payload.purpose == EQUIPMENT:
                    ledger.earmarked += payload.amount
                else:
                    ledger.free += payload.amount
            elif kind == "repayment":
                # 归还保障金：从普通桶划出，不足则只划可用部分
                take = min(ledger.free, payload.amount)
                ledger.free -= take
                payload.settled = take
            else:
                ob = payload
                amount = int(ob["amount"])
                used = {"earmarked": 0, "free": 0}
                remaining = amount
                if ob["purpose"] == EQUIPMENT:
                    take_em = min(ledger.earmarked, remaining)
                    ledger.earmarked -= take_em
                    used["earmarked"] += take_em
                    remaining -= take_em
                take_free = min(ledger.free, remaining)
                ledger.free -= take_free
                used["free"] += take_free
                remaining -= take_free
                paid = amount - remaining
                outcome = ObligationOutcome(
                    obligation_id=ob["id"],
                    club=ob["club"],
                    purpose=ob["purpose"],
                    rank=ob["rank"],
                    due=day,
                    amount=amount,
                    paid_on_time=paid,
                    shortfall=remaining,
                    bucket_used=used,
                    recovered_date=None,
                )
                ledger.outcomes.append(outcome)
                if remaining:
                    pending.append(outcome)

        # 日终：用当日剩余的可用现金尝试补齐历史断点。
        # 补付的现金通过 bucket_used 累加、shortfall 等额下降；义务的累计现金兑付
        # 恒等于 amount - shortfall（也等于两个 bucket_used 之和），保证现金守恒。
        for outcome in pending:
            if outcome.shortfall <= 0:
                continue
            if outcome.purpose == EQUIPMENT:
                take_em = min(ledger.earmarked, outcome.shortfall)
                ledger.earmarked -= take_em
                outcome.shortfall -= take_em
                outcome.bucket_used["earmarked"] += take_em
            take_free = min(ledger.free, outcome.shortfall)
            ledger.free -= take_free
            outcome.shortfall -= take_free
            outcome.bucket_used["free"] += take_free
            if outcome.shortfall == 0 and outcome.recovered_date is None:
                outcome.recovered_date = day

        ledger.timeline.append(
            {
                "date": day.isoformat(),
                "free": ledger.free,
                "earmarked": ledger.earmarked,
            }
        )

    # 按月汇总：月末仍有挂账短缺 = break；曾短缺但观察窗内补齐 = bridge；否则 ok
    month_acc: dict = {}
    for outcome in ledger.outcomes:
        mk = month_key(outcome.due)
        acc = month_acc.setdefault(mk, {"due": 0, "paid": 0, "short": 0, "open": 0})
        acc["due"] += outcome.amount
        acc["paid"] += outcome.paid_on_time
        acc["short"] += outcome.amount - outcome.paid_on_time
        acc["open"] += outcome.shortfall

    for mk in sorted(month_acc):
        acc = month_acc[mk]
        if acc["short"] == 0:
            status = "ok"
        elif acc["open"] == 0:
            status = "bridge"
        else:
            status = "break"
        end_free = next(
            (t["free"] for t in reversed(ledger.timeline) if t["date"][:7] == mk),
            ledger.free,
        )
        end_em = next(
            (t["earmarked"] for t in reversed(ledger.timeline) if t["date"][:7] == mk),
            ledger.earmarked,
        )
        ledger.months.append(
            MonthState(
                month=mk,
                free_end=end_free,
                earmarked_end=end_em,
                due_total=acc["due"],
                paid_on_time_total=acc["paid"],
                shortfall_total=acc["short"],
                status=status,
            )
        )
    return ledger


def build_receipts(views, projection) -> list[Receipt]:
    """把 (ReceivableView -> 预计到账日) 的投影结果转成现金日历入账事件。

    projection: dict[receivable_id -> date | None]，None 表示该款被撤销/不可依赖。
    已回款的按真实回执日入账。
    """
    receipts: list[Receipt] = []
    for view in views:
        row = view.row
        landing = projection.get(row["id"], "missing")
        if landing is None or landing == "missing":
            continue
        receipts.append(
            Receipt(
                date=landing,
                amount=int(row["amount"]),
                bucket=_bucket_for_receivable(row),
                source_id=row["id"],
                note="received" if view.received else "projected",
            )
        )
    return receipts
