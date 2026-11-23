# 俱乐部生存保障金的关联敞口与赛季拨付试算

日期采用当地营业日。预算盘为每赛季开始的来源快照，后续届期结合各自的已知资料时点。

records 保存可离线使用的合成业务记录。每行一个 UTF-8 JSON 对象，id 在各数据表内唯一，跨表键沿用业务记录中的标识。catalog.json 保存行数及原始文件校验值。

| 记录 | 业务含义 |
| --- | --- |
| clubs.jsonl | 金额均为约定的整数百元，earmarked 为期初设备专款。 |
| receivables.jsonl | 日期为年、月、日，received 为空是本批未见回执。 |
| obligations.jsonl | rank 越小越早受保障，专款只可用于匹配用途。 |
| payer_relations.jsonl | 真实生效日起关联付款组，known 是财务登记时间。 |
| charter.jsonl | 各用途的保障期、最低覆盖、归还规则和额度。 |
| resolutions.jsonl | confirmed 是已承诺，paid 表示已划款；归还要有独立回执。 |
| stress.jsonl | 同一付款组内同时发生的账期变化。 |

## 核对资料

运行环境为 Python 3.11，使用标准库，无需下载依赖。用 `python3 -m unittest discover -s tests -v` 核对发行资料的完整性与约定的输入关系。

## 压力试算与保障金拨付（treasury 包）

`treasury/` 在不改动发行资料的前提下，按"知识时点 as_of"重建每家俱乐部的现金月历，
并在同一付款集团共同延迟的压力情景下裁决保障金拨付。所有金额沿用整数百元。

### 口径纪律

- **两个资金桶**：普通桶（期初现金 + 普通赞助）可付任何义务；专款桶（期初设备专款 +
  `earmark=equipment` 回款）只能付 equipment 义务，普通桶不得为负。
- **四类应收同时区分**：已收现金（按回执日入账）、可撤销款项（压力中视为不可依赖）、
  承诺未到期、过期未付；未知集团归属的赞助方保留"可能同组/独立"两种影响范围。
- **as_of 时点**：集团更正只在 `known <= as_of` 时可见；回测只用 as_of 前的回执估计
  正常账期，`assert_no_lookahead` 硬性拦截前视信息。
- **断点分级**：`ok` 按时兑付；`bridge` 到期日有缺口但观察窗内被后续回款补齐；
  `break` 期末仍挂账的无保证现金断点。
- **拨付约束同时生效**：用途优先级与最低覆盖（payroll 100%、entry 80%、equipment 0）、
  俱乐部受援上限 `cap`、既有决议承诺不可再部署、资金池 `initial/replenishment/expiry`、
  按到期日逐笔分期而非整季平均；受益方自行回款后按 FIFO 归还并重新入池。
- **明示短缺**：资金池或上限挡下的部分逐笔列示阻塞约束，并与注入后复算的现金日历
  精确对账（测试 `test_final_ledger_due_shortfall_reconciles_exactly_with_explicit_list`）。

### 命令

```bash
# 发行完整性核对（sha256 + 行数）
python3 -I treasury/__main__.py verify --records records

# 单情景压力试算与拨付裁决（输出断点月份、新增拨付、明示短缺、后续可用余额）
python3 -I treasury/__main__.py project --records records \
    --season 2026 --as-of 2026-04-30 --scenario S-all

# 未知集团关系：加 --include-possible 把 unlinked sponsor 纳入可能同组口径
python3 -I treasury/__main__.py project --records records \
    --season 2026 --as-of 2026-04-30 --scenario S-unknown --include-possible

# 历史赛季回测（只用当时已知账期，事后回执仅用于评分）
python3 -I treasury/__main__.py backtest --records records \
    --season 2024 --as-of 2024-06-30 --scenario S-one

# 理事会报告（Markdown）+ 完整 JSON 证据包（逐笔可追查）
python3 -I treasury/__main__.py report --records records \
    --season 2026 --as-of 2026-04-30 --scenario S-all --out reports \
    --backtest-season 2024 --backtest-as-of 2024-06-30 --backtest-scenario S-one
```

### 模块

| 模块 | 职责 |
| --- | --- |
| `treasury/calendar.py` | 营业日期与月份工具 |
| `treasury/io_utils.py` | jsonl 读取与 catalog 校验 |
| `treasury/relations.py` | 付款集团关系的 as_of 解析与更正替换 |
| `treasury/knowledge.py` | 应收四类状态、决议可见性、前视守卫 |
| `treasury/stress.py` | 情景构建、正常账期估计、延迟/撤销投影 |
| `treasury/ledger.py` | 双桶现金日历、ok/bridge/break 判定 |
| `treasury/fund.py` | 拨付裁决、上限/最低覆盖/承诺/归还、复算对账 |
| `treasury/backtest.py` | 全链路组装与历史样本回测评分 |
| `treasury/report.py` | 理事会 Markdown 报告与 JSON 证据包 |

