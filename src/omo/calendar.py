"""交易日历: 节假日、周末与补班, 以及到期日推算。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class TradingCalendar:
    """历史日历。

    holidays: 法定节假日(即使落在工作日也不交易);
    makeup_workdays: 节假日调休后的补班日(即使是周末也交易)。
    """

    holidays: frozenset[date]
    makeup_workdays: frozenset[date]

    def is_trading_day(self, day: date) -> bool:
        if day in self.holidays:
            return False
        if day in self.makeup_workdays:
            return True
        return day.weekday() < 5

    def next_trading_day(self, day: date) -> date:
        candidate = day
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def maturity_date(self, start: date, term_days: int) -> date:
        """期限按自然日计, 到期日落在非交易日则顺延到下一交易日。"""
        if term_days < 0:
            raise ValueError("期限不能为负")
        return self.next_trading_day(start + timedelta(days=term_days))
