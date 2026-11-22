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

运行环境为 Python 3.11，使用标准库，无需下载依赖。用 `python3 -m unittest discover -v` 核对发行资料的完整性与约定的输入关系。
