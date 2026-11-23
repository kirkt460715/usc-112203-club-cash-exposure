"""营业日期工具。记录里的日期都是当地营业日，直接按日历日比较即可。"""
from __future__ import annotations

from datetime import date, timedelta

DATE_LEN = 3


def d(value) -> date:
    """把 [年, 月, 日] 数组、ISO 字符串或 date 转成 date。"""
    if isinstance(value, date):
        return value
    if value is None:
        raise ValueError("日期为空")
    if isinstance(value, str):
        return date.fromisoformat(value)
    if len(value) != DATE_LEN:
        raise ValueError(f"非法日期: {value!r}")
    return date(value[0], value[1], value[2])


def days_after(value, days: int) -> date:
    return d(value) + timedelta(days=days)


def month_key(value) -> str:
    value = d(value)
    return f"{value.year:04d}-{value.month:02d}"


def between(value, start, end) -> bool:
    value = d(value)
    if start is not None and value < d(start):
        return False
    if end is not None and value > d(end):
        return False
    return True
