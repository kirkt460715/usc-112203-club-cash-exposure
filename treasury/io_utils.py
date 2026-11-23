"""记录读取与发行完整性核对（对应 catalog.json 的行数与 sha256）。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

RECORD_NAMES = (
    "clubs",
    "receivables",
    "obligations",
    "payer_relations",
    "charter",
    "resolutions",
    "stress",
)


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def verify_catalog(records_dir: str | Path) -> dict:
    """核对每个 jsonl 的 sha256 与行数，返回 catalog；不通过则抛错。

    任何试算报告在生成前都应先通过本核对，保证引用的是同一批发行资料。
    """
    root = Path(records_dir)
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    for name, expected in catalog.items():
        raw = (root / f"{name}.jsonl").read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected["sha256"]:
            raise ValueError(f"{name}.jsonl 校验值不一致，资料可能被改动")
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if len(rows) != expected["rows"]:
            raise ValueError(f"{name}.jsonl 行数 {len(rows)} 与 catalog {expected['rows']} 不符")
        ids = [row["id"] for row in rows]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{name}.jsonl 存在重复 id")
    return catalog


def load_records(records_dir: str | Path, *, verify: bool = True) -> dict[str, list[dict]]:
    """加载全部业务记录；verify=True 时先做发行完整性核对。"""
    root = Path(records_dir)
    if verify:
        verify_catalog(root)
    return {name: _read_jsonl(root / f"{name}.jsonl") for name in RECORD_NAMES}
