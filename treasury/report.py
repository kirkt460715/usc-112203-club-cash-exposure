"""理事会报告：把试算结果整理成可追查的 Markdown + JSON 证据包。

报告固定回答六件事：
1. 哪些俱乐部在同一档期共同暴露于同一付款集团（关联敞口）；
2. 每家俱乐部每月是 ok / bridge（短期跨月可自愈）/ break（无保证现金断点）；
3. 共同付款风险情景与单家俱乐部变动的对照；
4. 保障金逐笔拨付选择、约束理由、明示短缺与阻塞约束；
5. 集团关系更正前后的敏感性差异；
6. 历史样本回测评分与既有决议核对、后续可用余额、未来或有缺口。
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .calendar import d, month_key
from .fund import FundRun, Tranche
from .relations import relation_corrections


def _json_default(obj):
    if isinstance(obj, set):
        return sorted(obj)
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"不可序列化: {type(obj)}")


def dump_evidence(run: dict, path: str | Path) -> Path:
    """把完整试算结果落为 JSON 证据包，供逐笔追查。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(run, ensure_ascii=False, indent=2, default=_json_default)
    path.write_text(payload, encoding="utf-8")
    return path


def _money(v: int) -> str:
    return f"{v:,}"


def exposure_section(records: dict, relation_map: dict) -> list[str]:
    lines = ["## 一、付款方关联敞口（同一集团审批的赞助方）", ""]
    groups: dict[str, list[str]] = {}
    unknown = []
    for sponsor, rel in sorted(relation_map.items()):
        if rel.payer_group is None:
            unknown.append(sponsor)
        else:
            groups.setdefault(rel.payer_group, []).append(sponsor)
    for group in sorted(groups):
        sponsors = sorted(groups[group])
        clubs = sorted(
            {
                r["club"]
                for r in records["receivables"]
                if r["sponsor"] in sponsors
            }
        )
        lines.append(
            f"- {group}：赞助方 {', '.join(sponsors)} → 受益俱乐部 {', '.join(clubs)}"
        )
    if unknown:
        lines.append(
            f"- 集团归属未知（影响范围保留）：{', '.join(sorted(unknown))}"
        )
    corrections = relation_corrections(records["payer_relations"])
    if corrections:
        lines.append("")
        lines.append("关系更正登记（敏感性来源）：")
        for c in corrections:
            lines.append(
                f"- {c['id']} 更正 {c['supersedes']}：{c['sponsor']} → {c['payer_group']}，"
                f"生效 {d(c['effective']).isoformat()}，财务登记 {d(c['known']).isoformat()}"
            )
    lines.append("")
    return lines


def _month_table(ledgers: dict) -> list[str]:
    lines = [
        "| 俱乐部 | 月份 | 普通桶期末 | 专款桶期末 | 到期义务 | 按时兑付 | 挂账短缺 | 状态 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for cid in sorted(ledgers):
        for m in ledgers[cid].months:
            lines.append(
                f"| {cid} | {m.month} | {_money(m.free_end)} | {_money(m.earmarked_end)} | "
                f"{_money(m.due_total)} | {_money(m.paid_on_time_total)} | "
                f"{_money(m.shortfall_total)} | {m.status} |"
            )
    lines.append("")
    return lines


def _common_gap_section(run: dict) -> list[str]:
    lines = ["## 二、共同付款风险下的现金月历（无保障金基线）", ""]
    scenario = run["projection"]["scenario"]
    lines.append(
        f"情景 `{scenario.id}`：集团 {list(scenario.groups)} 账期延迟 {scenario.delay_days} 天；"
        f"确定受影响赞助方 {sorted(run['projection']['certain_sponsors'])}；"
        f"未知关系可能受影响 {sorted(run['projection']['possible_sponsors'])}。"
    )
    lines.append("")
    lines.extend(_month_table(run["projection"]["ledgers"]))

    # 同一档期共同断点
    break_months: dict[str, list[str]] = {}
    bridge_months: dict[str, list[str]] = {}
    for cid, ledger in run["projection"]["ledgers"].items():
        for m in ledger.months:
            if m.status == "break":
                break_months.setdefault(m.month, []).append(cid)
            elif m.status == "bridge":
                bridge_months.setdefault(m.month, []).append(cid)
    lines.append("共同断点（同月出现无保证现金断点的俱乐部）：")
    for mk in sorted(break_months):
        lines.append(f"- {mk}：{', '.join(sorted(break_months[mk]))}")
    if not break_months:
        lines.append("- 无")
    lines.append("")
    lines.append("短期跨月可自愈（bridge，后续回款在观察窗内补齐）：")
    for mk in sorted(bridge_months):
        lines.append(f"- {mk}：{', '.join(sorted(bridge_months[mk]))}")
    if not bridge_months:
        lines.append("- 无")
    lines.append("")
    return lines


def _unknown_range_section(records: dict, run: dict, season: int) -> list[str]:
    """未知集团关系的两种影响范围对照：纳入 vs 不纳入"可能同组"的 sponsor。"""
    possible = run["projection"]["possible_sponsors"]
    if not possible:
        return []
    from .backtest import stress_run

    scenario_id = run["projection"]["scenario"].id
    as_of = run["projection"]["as_of"]

    def break_pairs(flag: bool):
        alt = stress_run(
            records, season, as_of, scenario_id, include_possible=flag
        )
        pairs = set()
        for cid, ledger in alt["projection"]["ledgers"].items():
            for m in ledger.months:
                if m.status == "break":
                    pairs.add((cid, m.month))
        short_total = sum(
            s["explicit_shortfall"] for s in alt["fund"].explicit_shortfalls
        )
        return pairs, short_total

    excluded, short_excluded = break_pairs(False)
    included, short_included = break_pairs(True)
    lines = ["### 未知集团关系的影响范围保留", ""]
    lines.append(
        f"赞助方 {sorted(possible)} 的集团归属未登记；下表给出两种口径，"
        "理事会应同时看到，不得假定其独立。"
    )
    lines.append("")
    lines.append("| 口径 | 断点俱乐部-月份数 | 明示短缺合计 |")
    lines.append("| --- | ---: | ---: |")
    lines.append(f"| 不纳入可能同组 | {len(excluded)} | {_money(short_excluded)} |")
    lines.append(f"| 纳入可能同组 | {len(included)} | {_money(short_included)} |")
    extra = sorted(included - excluded)
    if extra:
        lines.append("")
        lines.append("纳入后新增断点：" + "; ".join(f"{c} {m}" for c, m in extra))
    lines.append("")
    return lines


def _fund_section(fund: FundRun) -> list[str]:
    lines = ["## 三、保障金拨付选择（逐笔可追查）", ""]
    lines.append(
        f"资金池初始 {_money(fund.pool.initial)}，"
        f"补充 {[(d(r[:3]).isoformat(), r[3]) for r in fund.pool.replenishment]}，"
        f"到期 {d(fund.pool.expiry).isoformat()}；"
        f"已部署 {_money(fund.pool.deployed)}，累计归还 {_money(fund.pool.returned_total)}，"
        f"截至 as_of 后续可用 {_money(fund.pool.available)}。"
    )
    lines.append("")
    lines.append("| 拨付编号 | 俱乐部 | 义务 | 用途 | 到期日 | 金额 | 来源 | 已归还 | 阻塞约束 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | ---: | --- | ---: | --- | --- |")
    for t in fund.tranches:
        lines.append(
            f"| {t.id} | {t.club} | {t.obligation_id} | {t.purpose} | {d(t.due).isoformat()} | "
            f"{_money(t.amount)} | {t.source} | {_money(t.returned)} | "
            f"{t.blocking_constraint or ''} | {t.reason} |"
        )
    lines.append("")
    lines.append("### 明示短缺（保障金也无法保证按时兑付）")
    lines.append("")
    if fund.explicit_shortfalls:
        lines.append(
            "| 义务 | 俱乐部 | 用途 | 到期日 | 援助前缺口 | 已拨 | 到期未付 | 期末挂账 | 窗内恢复日 | 阻塞约束 |"
        )
        lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |")
        for s in fund.explicit_shortfalls:
            lines.append(
                f"| {s['obligation']} | {s['club']} | {s['purpose']} | {s['due']} | "
                f"{_money(s['uncovered_before_aid'])} | {_money(s['aid_granted'])} | "
                f"{_money(s['explicit_shortfall'])} | {_money(s['open_shortfall_at_horizon'])} | "
                f"{s['recovered_within_horizon'] or '—'} | "
                f"{', '.join(s['blocking_constraint'])} |"
            )
    else:
        lines.append("无。")
    lines.append("")
    lines.append("### 拨付后复算的现金月历")
    lines.append("")
    lines.extend(_month_table(fund.final_ledgers))
    lines.append("各俱乐部累计受援 / 受援上限：")
    lines.append("")
    lines.append("| 俱乐部 | 累计受援 | 上限 | 余量 |")
    lines.append("| --- | ---: | ---: | ---: |")
    return lines


def _cap_table(records: dict, fund: FundRun) -> list[str]:
    lines = []
    caps = {c["id"]: c["cap"] for c in records["clubs"]}
    for cid in sorted(caps):
        used = fund.club_aid.get(cid, 0)
        lines.append(
            f"| {cid} | {_money(used)} | {_money(caps[cid])} | {_money(caps[cid] - used)} |"
        )
    lines.append("")
    return lines


def _sensitivity_section(records: dict, season: int, as_of, scenario_id: str) -> list[str]:
    from .backtest import stress_run

    as_of = d(as_of)
    before = stress_run(
        records, season, as_of, scenario_id, use_corrections=False
    )
    after = stress_run(
        records, season, as_of, scenario_id, use_corrections=True
    )

    def breaks(run):
        out = {}
        for cid, ledger in run["projection"]["ledgers"].items():
            months = [m.month for m in ledger.months if m.status == "break"]
            if months:
                out[cid] = months
        return out

    b_before, b_after = breaks(before), breaks(after)
    lines = ["## 四、集团关系更正的敏感性", ""]
    lines.append(f"情景 `{scenario_id}`，知识时点 {as_of.isoformat()}。")
    lines.append("")
    lines.append("| 口径 | 断点俱乐部-月份 | 明示短缺笔数 | 明示短缺合计 | 新增拨付笔数 |")
    lines.append("| --- | --- | ---: | ---: | ---: |")
    for label, run, bmap in (
        ("忽略更正（旧关系图）", before, b_before),
        ("采纳更正（新关系图）", after, b_after),
    ):
        short_total = sum(s["explicit_shortfall"] for s in run["fund"].explicit_shortfalls)
        new_alloc = sum(1 for t in run["fund"].tranches if t.source == "new_allocation")
        flat = "; ".join(f"{c}: {','.join(ms)}" for c, ms in sorted(bmap.items())) or "无"
        lines.append(
            f"| {label} | {flat} | {len(run['fund'].explicit_shortfalls)} | "
            f"{_money(short_total)} | {new_alloc} |"
        )
    lines.append("")
    pairs_before = {(c, m) for c, ms in b_before.items() for m in ms}
    pairs_after = {(c, m) for c, ms in b_after.items() for m in ms}
    new_pairs = sorted(pairs_after - pairs_before)
    if new_pairs:
        lines.append("更正后落入同一档期的新增断点（俱乐部-月份）：")
        for cid, mk in new_pairs:
            lines.append(f"- {cid} {mk}")
    else:
        lines.append("更正后未产生新的断点月份。")
    lines.append(
        "注：既有决议的拨付已承诺，资金不可再次部署；重排只影响新增拨付的先后与额度。"
    )
    lines.append("")
    return lines


def _backtest_section(score: dict) -> list[str]:
    lines = ["## 五、历史样本回测（按当时已知账期检验）", ""]
    lines.append(
        f"赛季 {score['season']}，知识时点 {score['as_of']}，情景 `{score['scenario']}`，"
        f"样本 {score['sample_size']} 笔。"
    )
    lines.append("| 指标 | 笔数 | 含义 |")
    lines.append("| --- | ---: | --- |")
    lines.append(f"| 投影与真实回执同日 | {score['exact']} | 当时判断准确 |")
    lines.append(f"| 时点有偏差 | {score['off_by_timing']} | 方向一致、账期估计偏差 |")
    lines.append(f"| 真实仍未回款 | {score['truth_unpaid']} | 截至资料批次无回执 |")
    lines.append(f"| 压力中视为可撤销 | {score['projected_revoked']} | 可撤销款项未计入可用现金 |")
    lines.append("")
    lines.append(
        "回测只使用 as_of 前登记的关系与回执；事后真实数据仅用于评分，未进入当时判断。"
    )
    lines.append("")
    return lines


def build_board_report(
    records: dict,
    run: dict,
    *,
    season: int,
    as_of,
    scenario_id: str,
    backtest_score: dict | None = None,
) -> str:
    as_of = d(as_of)
    relation_map = run["projection"]["relations"]
    parts = [
        f"# 俱乐部生存保障金压力试算与拨付建议（赛季 {season}）",
        "",
        f"- 知识时点 as_of：{as_of.isoformat()}",
        f"- 观察窗：{as_of.isoformat()} 至 {d(run['projection']['horizon_end']).isoformat()}",
        f"- 压力情景：{scenario_id}",
        "- 金额单位：整数百元；日期为当地营业日",
        "",
    ]
    parts += exposure_section(records, relation_map)
    parts += _common_gap_section(run)
    parts += _unknown_range_section(records, run, season)
    parts += _fund_section(run["fund"])
    parts += _cap_table(records, run["fund"])
    parts += _sensitivity_section(records, season, as_of, scenario_id)
    if backtest_score:
        parts += _backtest_section(backtest_score)
    parts.append("## 六、既有决议与未来或有缺口")
    parts.append("")
    parts.append(
        "- 旧会议通过的拨付依据保留在决议表 resolutions.jsonl，"
        "本报告拨付编号可回溯到决议 id 与义务 id。"
    )
    parts.append(
        f"- 后续可用余额（含已归还）：{_money(run['fund'].pool.available)}；"
        "未来或有缺口见第二节共同断点与第三节明示短缺。"
    )
    parts.append("")
    return "\n".join(parts)
