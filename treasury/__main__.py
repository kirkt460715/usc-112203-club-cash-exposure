"""命令行入口：

python3 -m treasury verify  --records records
python3 -m treasury project --records records --season 2026 --as-of 2026-04-30 \
        --scenario S-all [--include-possible] [--no-corrections]
python3 -m treasury backtest --records records --season 2024 --as-of 2024-06-30 \
        --scenario S-one
python3 -m treasury report --records records --season 2026 --as-of 2026-04-30 \
        --scenario S-all --out reports/
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# 兼容 `python3 -I treasury/__main__.py` 直跑：隔离模式不读 PYTHONPATH，
# 需手动把工作区根目录加入 sys.path 后再使用包内相对导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from treasury.backtest import backtest_historical, stress_run
    from treasury.io_utils import load_records, verify_catalog
    from treasury.report import build_board_report, dump_evidence
else:
    from .backtest import backtest_historical, stress_run
    from .io_utils import load_records, verify_catalog
    from .report import build_board_report, dump_evidence


def _parse_date(text: str):
    return date.fromisoformat(text)


def _dataclass_to_jsonable(obj):
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(type(obj))


def main(argv=None) -> int:
    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument("--records", default="records", help="records 目录")
    parser = argparse.ArgumentParser(prog="treasury", description="俱乐部保障金压力试算",
                                 parents=[common_parent])
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("verify", parents=[common_parent], help="核对发行资料完整性")

    def add_common(p):
        p.add_argument("--season", type=int, required=True)
        p.add_argument("--as-of", type=_parse_date, required=True)
        p.add_argument("--scenario", required=True)
        p.add_argument("--include-possible", action="store_true")
        p.add_argument("--no-corrections", action="store_true")
        p.add_argument("--horizon", type=_parse_date, default=None)

    p_proj = sub.add_parser("project", parents=[common_parent], help="单情景压力试算与拨付裁决")
    add_common(p_proj)

    p_bt = sub.add_parser("backtest", parents=[common_parent], help="历史赛季回测评分")
    add_common(p_bt)

    p_rep = sub.add_parser("report", parents=[common_parent], help="生成理事会 Markdown 报告与 JSON 证据包")
    add_common(p_rep)
    p_rep.add_argument("--out", default="reports")
    p_rep.add_argument(
        "--backtest-season",
        type=int,
        default=None,
        help="附带历史回测的赛季（默认取试算赛季前两年）",
    )
    p_rep.add_argument("--backtest-as-of", type=_parse_date, default=None)
    p_rep.add_argument(
        "--backtest-scenario",
        default=None,
        help="历史回测使用的压力情景（默认与主情景相同）",
    )

    args = parser.parse_args(argv)
    records = load_records(args.records)

    if args.cmd == "verify":
        catalog = verify_catalog(args.records)
        print(json.dumps({"verified": sorted(catalog)}, ensure_ascii=False, indent=2))
        return 0

    kwargs = {
        "include_possible": args.include_possible,
        "use_corrections": not args.no_corrections,
        "horizon_end": args.horizon,
    }

    if args.cmd == "backtest":
        run = backtest_historical(
            records, args.season, args.__dict__["as_of"], args.scenario,
            include_possible=args.include_possible,
        )
        print(json.dumps(run["backtest_score"], ensure_ascii=False, indent=2))
        return 0

    run = stress_run(
        records,
        args.season,
        args.__dict__["as_of"],
        args.scenario,
        **kwargs,
    )

    if args.cmd == "project":
        summary = {
            "scenario": args.scenario,
            "as_of": args.__dict__["as_of"].isoformat(),
            "break_months": {
                cid: [m.month for m in ledger.months if m.status == "break"]
                for cid, ledger in run["projection"]["ledgers"].items()
            },
            "bridge_months": {
                cid: [m.month for m in ledger.months if m.status == "bridge"]
                for cid, ledger in run["projection"]["ledgers"].items()
            },
            "new_allocations": [
                {
                    "id": t.id,
                    "club": t.club,
                    "obligation": t.obligation_id,
                    "amount": t.amount,
                    "purpose": t.purpose,
                }
                for t in run["fund"].tranches
                if t.source == "new_allocation"
            ],
            "explicit_shortfalls": run["fund"].explicit_shortfalls,
            "pool_available": run["fund"].pool.available,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    # report
    out_dir = Path(args.out)
    bt_score = None
    bt_season = args.backtest_season or args.season - 2
    bt_as_of = args.backtest_as_of or date(bt_season, 6, 30)
    bt_scenario = args.backtest_scenario or args.scenario
    if bt_season in {r["season"] for r in records["receivables"]}:
        bt = backtest_historical(
            records,
            bt_season,
            bt_as_of,
            bt_scenario,
            include_possible=args.include_possible,
        )
        bt_score = bt["backtest_score"]
    evidence_path = out_dir / f"evidence_{args.season}_{args.scenario}.json"
    dump_evidence(run, evidence_path)
    md = build_board_report(
        records,
        run,
        season=args.season,
        as_of=args.__dict__["as_of"],
        scenario_id=args.scenario,
        backtest_score=bt_score,
    )
    md_path = out_dir / f"board_report_{args.season}_{args.scenario}.md"
    md_path.write_text(md, encoding="utf-8")
    print(json.dumps({"report": str(md_path), "evidence": str(evidence_path)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
