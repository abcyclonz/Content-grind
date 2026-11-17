"""Daily budget cap per channel (SPEC section 3, principle 6)."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from network import db
from network.config import ChannelConfig


class BudgetExceeded(RuntimeError):
    pass


def local_day_start(channel: ChannelConfig, now: datetime | None = None) -> datetime:
    now = (now or datetime.now(channel.tz)).astimezone(channel.tz)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def spent_today(conn: sqlite3.Connection, channel: ChannelConfig) -> float:
    return db.spent_since(conn, channel.id, local_day_start(channel))


def check_budget(conn: sqlite3.Connection, channel: ChannelConfig) -> float:
    """Raise if today's spend has reached the cap; otherwise return what is left."""
    spent = spent_today(conn, channel)
    left = channel.daily_budget_usd - spent
    if left <= 0:
        raise BudgetExceeded(
            f"{channel.id}: daily budget ${channel.daily_budget_usd:.2f} reached "
            f"(spent ${spent:.2f} today, {channel.timezone})"
        )
    return left
