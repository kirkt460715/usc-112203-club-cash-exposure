"""保障金拨付引擎：在现金日历证明的缺口上决定"哪笔钱现在能用"。

裁决约束（同时生效，缺一不可）：
1. 章程用途优先级 payroll > entry > equipment，专款只能用于匹配用途；
2. 最低可保障范围 minimum_cover（payroll=100%、entry=80%、equipment=0）；
3. 每家具乐部受援上限 cap；
4. 既有决议已承诺/已划款的钱不可再次部署，只登记不重排；
5. 资金池受 initial、replenishment（到日才可动用）与 expiry 限制；
6. 拨付按到期日 + rank 排序，分期、逐笔挂到具体义务上，不按整季平均切；
7. return_on_receipt：受益方自行回款后按 FIFO 归还，归还额重新进入资金池，
   但不追溯改变更早义务的兑付结果。

无法满足的部分必须明示为 explicit_shortfall，并写明是哪条约束挡住的。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction

from .calendar import d
from .ledger import Injection, ObligationOutcome, Repayment, simulate_club

CEIL = Fraction(1, 1)


def _ceil_cover(amount: int, ratio: float) -> int:
    """向上取整的最低覆盖额，保证覆盖比例不低于章程要求。"""
    need = Fraction(amount) * Fraction(str(ratio))
    whole = need.numerator // need.denominator
    return whole + (1 if need.numerator % need.denominator else 0)


@dataclass
class Tranche:
    id: str
    club: str
    obligation_id: str
    purpose: str
    due: object
    amount: int
    released_date: object
    source: str  # "resolution" | "new_allocation"
    resolution_id: str | None
    returned: int = 0
    return_events: list = field(default_factory=list)
    reason: str = ""
    # 未被满足时的明示短缺
    shortfall_after: int = 0
    blocking_constraint: str | None = None


@dataclass
class PoolState:
    initial: int
    replenishment: list
    expiry: object
    available: int
    deployed: int
    returned_total: int


@dataclass
class FundRun:
    pool: PoolState
    tranches: list
    explicit_shortfalls: list
    decision_log: list
    club_aid: dict
    projected_ledgers: dict
    final_ledgers: dict
    scenario_id: str
    as_of: object
    horizon_end: object


def _pool_available_on(pool_cash: int, replenishment: list, day) -> int:
    # 每行结构为 [年, 月, 日, 金额]
    extra = sum(item[3] for item in replenishment if d(item[:3]) <= d(day))
    return pool_cash + extra


def _resolution_tranches(resolution_rows, as_of, obligation_index) -> list[Tranche]:
    as_of = d(as_of)
    out = []
    for r in resolution_rows:
        if d(r["approved"]) > as_of:
            continue
        ob = obligation_index.get((r["club"], r["purpose"], d(r["due"])))
        out.append(
            Tranche(
                id=f"T-{r['id']}",
                club=r["club"],
                obligation_id=ob["id"] if ob else f"{r['club']}:{r['purpose']}",
                purpose=r["purpose"],
                due=d(r["due"]),
                amount=int(r["amount"]),
                released_date=d(r["approved"]),
                source="resolution",
                resolution_id=r["id"],
                reason=f"既有决议 {r['id']}（{r['status']}），资金已承诺不可再部署",
            )
        )
    return out


def run_fund(
    records: dict,
    projected_ledgers: dict,
    projections_by_club: dict,
    receipts_by_club: dict,
    scenario_id: str,
    as_of,
    horizon_end,
    season: int,
) -> FundRun:
    """在无保障金的 projected_ledgers 之上做拨付裁决，并用注入后复算验证。

    projections_by_club[club] = {receivable_id: 预计到账 date/None}
    receipts_by_club[club] = [Receipt,...]（与投影一致，用于回款归还排程）
    """
    as_of = d(as_of)
    horizon_end = d(horizon_end)
    charter = records["charter"][0]
    clubs = {c["id"]: c for c in records["clubs"]}
    priority = charter["priority"]
    min_cover = charter["minimum_cover"]
    expiry = d(charter["expiry"])
    horizon = min(horizon_end, expiry)

    # 两个义务窗口：
    # - 全季窗口用于与投影日历对齐、建立义务索引与复算；
    # - 拨款窗口（due >= as_of）：保障金不能为 as_of 之前已到期的义务追溯拨款，
    #   那些历史断点只在基线月历中体现，不进入本轮拨付裁决。
    season_start = date(season, 1, 1)
    all_obligation_rows = [
        o for o in records["obligations"]
        if season_start <= d(o["due"]) <= horizon
    ]
    obligation_rows = [o for o in all_obligation_rows if d(o["due"]) >= as_of]
    obligation_index = {(o["club"], o["purpose"], d(o["due"])): o for o in all_obligation_rows}
    base_outcomes: dict[str, ObligationOutcome] = {}
    for club, ledger in projected_ledgers.items():
        for outcome in ledger.outcomes:
            base_outcomes[outcome.obligation_id] = outcome

    tranches: list[Tranche] = _resolution_tranches(
        records["resolutions"], as_of, obligation_index
    )
    committed_by_club = {cid: 0 for cid in clubs}
    for t in tranches:
        committed_by_club[t.club] += t.amount

    decision_log = [
        {
            "step": "open",
            "pool_initial": charter["initial"],
            "replenishment": charter["replenishment"],
            "expiry": expiry.isoformat(),
            "committed_resolutions": [
                {"id": t.resolution_id, "club": t.club, "amount": t.amount}
                for t in tranches
            ],
        }
    ]

    pool_cash = int(charter["initial"])
    # 既有已承诺金额在资金池中预留（approved 即不可再部署）
    pool_cash -= sum(t.amount for t in tranches)
    if pool_cash < 0:
        raise ValueError("既有决议承诺超过保障金初始额度")

    def priority_key(ob):
        due = d(ob["due"])
        replenished = _pool_available_on(0, charter["replenishment"], due)
        return (due, priority.index(ob["purpose"]), ob["rank"], ob["club"], replenished)

    ordered = sorted(obligation_rows, key=priority_key)
    # 分配阶段的候选记录；明示短缺在注入复算后按真实兑付结果回填
    allocation_records = []
    club_aid = {cid: committed_by_club[cid] for cid in clubs}

    for ob in ordered:
        due = d(ob["due"])
        base = base_outcomes.get(ob["id"])
        # 缺口按"到期日未付额"口径（amount - paid_on_time），而非期末挂账：
        # bridge 义务虽然后续回款补齐，但到期日仍未付清，保障金若要保证按时履约
        # 仍应介入；期末挂账（shortfall）只用于区分 bridge 与 break。
        uncovered = (base.amount - base.paid_on_time) if base else int(ob["amount"])
        if uncovered <= 0:
            decision_log.append(
                {
                    "step": "skip_covered",
                    "obligation": ob["id"],
                    "club": ob["club"],
                    "reason": "俱乐部自有资金可按时覆盖，无需保障金",
                }
            )
            continue

        minimum = _ceil_cover(int(ob["amount"]), min_cover[ob["purpose"]])
        # 需求取"实际缺口"与"章程最低覆盖额"的较大者；两者都不会超过义务全额
        demand = min(max(uncovered, minimum), int(ob["amount"]))

        cap_room = int(clubs[ob["club"]]["cap"]) - club_aid[ob["club"]]
        pool_room = _pool_available_on(pool_cash, charter["replenishment"], due)
        granted = min(demand, max(cap_room, 0), max(pool_room, 0))

        blockers = []
        if cap_room < demand:
            blockers.append("cap")
        if pool_room < demand:
            blockers.append("pool")

        record = {
            "obligation_row": ob,
            "uncovered": uncovered,
            "minimum": minimum,
            "cap_room": cap_room,
            "pool_room": pool_room,
            "granted": granted,
            "blockers": blockers,
        }
        allocation_records.append(record)

        if granted > 0:
            pool_cash -= granted
            club_aid[ob["club"]] += granted
            tranche = Tranche(
                id=f"T-{scenario_id}-{len(tranches) + 1}",
                club=ob["club"],
                obligation_id=ob["id"],
                purpose=ob["purpose"],
                due=due,
                amount=granted,
                released_date=due,
                source="new_allocation",
                resolution_id=None,
                reason=(
                    f"按 {due.isoformat()} 到期义务拨付，用途 {ob['purpose']}，"
                    f"基线缺口 {uncovered}，约束后实拨 {granted}"
                ),
                blocking_constraint=",".join(blockers) if blockers else None,
            )
            tranches.append(tranche)
            decision_log.append(
                {
                    "step": "allocate",
                    "obligation": ob["id"],
                    "club": ob["club"],
                    "purpose": ob["purpose"],
                    "due": due.isoformat(),
                    "uncovered": uncovered,
                    "minimum_cover": minimum,
                    "cap_room": cap_room,
                    "pool_room_before": pool_room,
                    "granted": granted,
                    "blockers": blockers,
                }
            )

    # ---- 回款归还：受益方自行回款后按 FIFO 归还，重新入池 ----
    # 先排程归还事件；实际划扣在复算现金日历时作为俱乐部现金流出入账，
    # 保证"俱乐部桶减少 == 资金池增加"，两侧同时守恒。
    returned_total = 0
    repayments_by_club: dict[str, list[dict]] = {cid: [] for cid in clubs}
    if charter.get("return_on_receipt"):
        open_tranches = [t for t in tranches if t.source == "new_allocation"]
        for club in clubs:
            queue = [t for t in open_tranches if t.club == club]
            for rc in sorted(receipts_by_club.get(club, []), key=lambda r: d(r.date)):
                if rc.note != "projected":
                    continue
                repay_budget = rc.amount
                for t in queue:
                    if repay_budget <= 0:
                        break
                    outstanding = t.amount - t.returned
                    if outstanding <= 0:
                        continue
                    pay = min(repay_budget, outstanding)
                    t.returned += pay
                    t.return_events.append({"date": rc.date.isoformat(), "amount": pay})
                    repay_budget -= pay
                    returned_total += pay
                    pool_cash += pay
                    repayments_by_club[club].append(
                        {"date": d(rc.date), "amount": pay, "tranche": t.id,
                         "receipt": rc.source_id}
                    )
                    decision_log.append(
                        {
                            "step": "return",
                            "tranche": t.id,
                            "club": club,
                            "receipt": rc.source_id,
                            "amount": pay,
                            "date": rc.date.isoformat(),
                        }
                    )

    # ---- 注入后复算，验证哪些义务仍能及时兑付、哪些明示短缺 ----
    final_ledgers = {}
    injections_by_club: dict[str, list[Injection]] = {cid: [] for cid in clubs}
    for t in tranches:
        injections_by_club[t.club].append(
            Injection(
                date=t.released_date,
                amount=t.amount,
                purpose=t.purpose,
                source_id=t.id,
            )
        )
    repayment_objs_by_club: dict[str, list] = {cid: [] for cid in clubs}
    for cid, events in repayments_by_club.items():
        for ev in events:
            repayment_objs_by_club[cid].append(
                Repayment(date=ev["date"], amount=ev["amount"], source_id=ev["tranche"])
            )
    for cid, club_row in clubs.items():
        obs = [
            o
            for o in records["obligations"]
            if o["club"] == cid and season_start <= d(o["due"]) <= horizon
        ]
        final_ledgers[cid] = simulate_club(
            club_row,
            obs,
            receipts_by_club.get(cid, []),
            horizon,
            injections_by_club[cid],
            repayment_objs_by_club[cid],
        )

    # 归还以俱乐部普通桶实际划出额为准；未划出部分保留为池的应收（不重复计入可用）
    settled_by_tranche: dict[str, int] = {}
    for rps in repayment_objs_by_club.values():
        for rp in rps:
            settled_by_tranche[rp.source_id] = (
                settled_by_tranche.get(rp.source_id, 0) + rp.settled
            )
    scheduled_total = sum(t.returned for t in tranches)
    settled_returns = sum(settled_by_tranche.values())
    if settled_returns != scheduled_total:
        # 排程额超出俱乐部可用现金：按 tranche 冲回多计部分，保留为池的应收未收
        for t in tranches:
            settled = settled_by_tranche.get(t.id, 0)
            if settled < t.returned:
                t.return_events.append(
                    {"date": None, "amount": t.returned - settled, "note": "unsettled"}
                )
                t.returned = settled
        pool_cash -= scheduled_total - settled_returns
        returned_total = settled_returns

    # ---- 复算后回填明示短缺：以注入后现金日历的真实到期未付为准 ----
    final_outcomes: dict[str, ObligationOutcome] = {}
    for ledger in final_ledgers.values():
        for outcome in ledger.outcomes:
            final_outcomes[outcome.obligation_id] = outcome

    explicit_shortfalls = []
    tranche_by_ob: dict[str, Tranche] = {t.obligation_id: t for t in tranches}
    for rec in allocation_records:
        ob = rec["obligation_row"]
        final_ob = final_outcomes.get(ob["id"])
        due_shortfall = (
            final_ob.amount - final_ob.paid_on_time if final_ob else int(ob["amount"])
        )
        if due_shortfall <= 0:
            continue
        granted = rec["granted"]
        blockers = list(rec["blockers"]) or ["no_eligible_cash"]
        explicit_shortfalls.append(
            {
                "obligation": ob["id"],
                "club": ob["club"],
                "purpose": ob["purpose"],
                "due": d(ob["due"]).isoformat(),
                "uncovered_before_aid": rec["uncovered"],
                "aid_granted": granted,
                "explicit_shortfall": due_shortfall,
                "open_shortfall_at_horizon": final_ob.shortfall if final_ob else due_shortfall,
                "recovered_within_horizon": (
                    final_ob.recovered_date.isoformat()
                    if final_ob and final_ob.recovered_date
                    else None
                ),
                "blocking_constraint": blockers,
                "minimum_required": rec["minimum"],
            }
        )
        decision_log.append(
            {
                "step": "explicit_shortfall",
                "obligation": ob["id"],
                "club": ob["club"],
                "due_shortfall": due_shortfall,
                "open_at_horizon": final_ob.shortfall if final_ob else due_shortfall,
                "blockers": blockers,
            }
        )
        if ob["id"] in tranche_by_ob:
            tranche_by_ob[ob["id"]].shortfall_after = due_shortfall

    available_now = _pool_available_on(pool_cash, charter["replenishment"], as_of)
    pool = PoolState(
        initial=int(charter["initial"]),
        replenishment=charter["replenishment"],
        expiry=expiry,
        available=available_now,
        deployed=sum(t.amount for t in tranches),
        returned_total=returned_total,
    )
    return FundRun(
        pool=pool,
        tranches=tranches,
        explicit_shortfalls=explicit_shortfalls,
        decision_log=decision_log,
        club_aid=club_aid,
        projected_ledgers=projected_ledgers,
        final_ledgers=final_ledgers,
        scenario_id=scenario_id,
        as_of=as_of,
        horizon_end=horizon,
    )
