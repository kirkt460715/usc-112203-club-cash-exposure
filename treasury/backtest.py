"""把引擎组装成一次完整试算的高层函数，以及历史样本回测。

回测纪律（对应需求"按当时已知账期检验，不能用后来回款假装当时更准"）：
- 关系图只用 known <= as_of 的登记；
- 应收视图只用 signed <= as_of 的合同，已收现金只认 received <= as_of；
- 正常账期只用 as_of 前的回执样本估计；
- assert_no_lookahead 作为硬性守卫，发现前视信息直接抛错。
"""
from __future__ import annotations

from datetime import date

from .calendar import d
from .fund import run_fund
from .knowledge import (
    assert_no_lookahead,
    known_resolutions,
    visible_receivables,
)
from .ledger import build_receipts, simulate_club
from .relations import active_relations
from .stress import (
    Scenario,
    affected_sponsors,
    build_scenarios,
    normal_lag_by_sponsor,
    projected_receipt_date,
)


def project_season(
    records: dict,
    season: int,
    as_of,
    scenario: Scenario | None,
    *,
    include_possible: bool = False,
    use_corrections: bool = True,
    horizon_end=None,
):
    """对一个赛季跑全部俱乐部的无保障金现金日历。

    返回 dict：relations、views/projections/receipts/ledgers、情景元数据。
    use_corrections=False 时忽略 as_of 前已登记的更正（敏感性分析用）。
    """
    as_of = d(as_of)
    relation_rows = records["payer_relations"]
    if not use_corrections:
        relation_rows = [r for r in relation_rows if not r.get("supersedes")]
    relations = active_relations(relation_rows, as_of)
    normal_lags = normal_lag_by_sponsor(records["receivables"], as_of)

    season_rows = [r for r in records["receivables"] if r["season"] == season]
    obligation_rows_all = [
        o for o in records["obligations"] if o["id"].startswith(f"O{season}-")
    ]
    if horizon_end is None:
        horizon_end = d(records["charter"][0]["expiry"])
    horizon_end = d(horizon_end)
    # 现金日历从赛季初开始模拟：as_of 前的已收回款按真实回执日入账，
    # 与期初 start_cash 衔接，避免把已收现金在 as_of 重复计一次。
    season_start = date(season, 1, 1)

    clubs = {c["id"]: c for c in records["clubs"]}
    ledgers, projections, receipts_all, views_all = {}, {}, {}, {}
    certain, possible = (
        affected_sponsors(scenario, relations) if scenario else (set(), set())
    )

    for cid, club_row in clubs.items():
        # 视图仍按 as_of 过滤（未签约合同不可见），但日历从赛季初排起
        views = [v for v in visible_receivables(season_rows, as_of) if v.row["club"] == cid]
        assert_no_lookahead(views, as_of)
        projection = {}
        for v in views:
            projection[v.row["id"]] = projected_receipt_date(
                v,
                scenario,
                relations,
                normal_lags,
                include_possible=include_possible,
            )
        receipts = build_receipts(views, projection)
        obs = [
            o
            for o in obligation_rows_all
            if o["club"] == cid
            and season_start <= d(o["due"]) <= horizon_end
        ]
        ledgers[cid] = simulate_club(club_row, obs, receipts, horizon_end)
        projections[cid] = projection
        receipts_all[cid] = receipts
        views_all[cid] = views

    return {
        "season": season,
        "as_of": as_of,
        "horizon_end": horizon_end,
        "relations": relations,
        "scenario": scenario,
        "certain_sponsors": certain,
        "possible_sponsors": possible,
        "views": views_all,
        "projections": projections,
        "receipts": receipts_all,
        "ledgers": ledgers,
        "normal_lags": normal_lags,
    }


def stress_run(
    records: dict,
    season: int,
    as_of,
    scenario_id: str,
    *,
    include_possible: bool = False,
    use_corrections: bool = True,
    horizon_end=None,
):
    """无保障金压力试算 + 保障金拨付裁决的完整链路。"""
    scenarios = {s.id: s for s in build_scenarios(records["stress"])}
    scenario = scenarios[scenario_id]
    projection = project_season(
        records,
        season,
        as_of,
        scenario,
        include_possible=include_possible,
        use_corrections=use_corrections,
        horizon_end=horizon_end,
    )
    fund_run = run_fund(
        records,
        projection["ledgers"],
        projection["projections"],
        projection["receipts"],
        scenario_id=scenario_id,
        as_of=as_of,
        horizon_end=projection["horizon_end"],
        season=season,
    )
    return {"projection": projection, "fund": fund_run}


def backtest_historical(
    records: dict,
    season: int,
    as_of,
    scenario_id: str,
    *,
    include_possible: bool = False,
):
    """用历史赛季做样本检验：同一套 as_of 纪律跑压力试算。

    历史赛季没有保障金决议，拨付引擎会在空决议下运行，
    因此其结论完全来自当时可见的账期与关系，可与后来的真实回执对照评分。
    """
    as_of = d(as_of)
    season_receivables = [r for r in records["receivables"] if r["season"] == season]
    later_truth = {
        r["id"]: (d(r["received"]) if r["received"] else None)
        for r in season_receivables
    }
    run = stress_run(
        records,
        season,
        as_of,
        scenario_id,
        include_possible=include_possible,
    )
    # 评分：投影到账日 vs 后来真实回执（仅用于事后评分，不参与当时判断）
    hits, late_hits, missed, revoked = 0, 0, 0, 0
    detail = []
    for cid, views in run["projection"]["views"].items():
        for v in views:
            rid = v.row["id"]
            projected = run["projection"]["projections"][cid][rid]
            truth = later_truth.get(rid)
            if projected is None:
                revoked += 1
                tag = "projected_revoked"
            elif truth is None:
                missed += 1
                tag = "truth_unpaid"
            elif projected == truth:
                hits += 1
                tag = "exact"
            elif projected > truth:
                late_hits += 1
                tag = "projected_late"
            else:
                late_hits += 1
                tag = "projected_early"
            detail.append(
                {
                    "receivable": rid,
                    "club": cid,
                    "projected": projected.isoformat() if projected else None,
                    "truth": truth.isoformat() if truth else None,
                    "tag": tag,
                }
            )
    run["backtest_score"] = {
        "season": season,
        "as_of": as_of.isoformat(),
        "scenario": scenario_id,
        "exact": hits,
        "off_by_timing": late_hits,
        "truth_unpaid": missed,
        "projected_revoked": revoked,
        "sample_size": len(detail),
    }
    run["backtest_detail"] = detail
    return run
