"""ZCode 用量源 + ZCode 数据落点的唯一定义。

ZCODE_DIR/DB_PATH/connect_ro/today0_ms 自 data_engine 下沉到本模块
(单一定义):data_engine 模块级 re-import 保住全部旧导入路径
(from zcode_meter.data_engine import DB_PATH 等历史用法零改动),测试
patch 本模块全局即可单点生效。类与 SQL 自 data_engine 原样迁移,一字未动。

红线:严禁在本包内 import data_engine(见 base.py 模块 docstring)。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3

from .base import UsageSource

ZCODE_DIR = os.path.expanduser("~/.zcode/cli")
DB_PATH = os.path.join(ZCODE_DIR, "db", "db.sqlite")


def connect_ro() -> sqlite3.Connection:
    """只读连接:引擎轮询与 UI 侧图表/菜单查询共用,统一 open 参数。"""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)


def today0_ms() -> int:
    """本地午夜毫秒时间戳:今日用量(_poll_stats)与按天图表(fetch_daily_usage)
    必须共用同一午夜口径,否则两张图对不上账。"""
    return int(dt.datetime.now().replace(hour=0, minute=0, second=0,
                                         microsecond=0).timestamp() * 1000)


class ZCodeSource(UsageSource):
    """默认源:包装现有 ZCode SQLite 口径。today_usage 的 SQL 与 _poll_stats
    的今日用量完全同 WHERE 同天界(全来源 completed in+out)—— 两处刻意
    保持同文,单测 test_today_by_source 对账防漂移。"""

    name = "ZCode"
    # discover_sources 排序键(order 小者在前):0 = 恒首位,卡片
    # today_by_source 的显示顺序与迁移前 [ZCode, Claude] 保持不变
    order = 0

    def is_available(self) -> bool:
        return os.path.exists(DB_PATH)

    def today_usage(self) -> int:
        try:
            con = connect_ro()
            (t,) = con.execute(
                "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?",
                (today0_ms(),)).fetchone()
            con.close()
            return t or 0
        except sqlite3.Error:
            return 0

    def daily_usage(self, days: int = 30) -> list:
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT date(started_at/1000, 'unixepoch', 'localtime') AS d,"
                " COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " GROUP BY d ORDER BY d",
                (today0_ms() - (days - 1) * 86_400_000,)).fetchall()
            con.close()
            return rows
        except sqlite3.Error:
            return []


# discover_sources() 约定:模块级 Source 属性指向本模块的源类(README
# 『如何贡献一个源』)—— 类名本身随意,发现机制只认 Source 这个名字
Source = ZCodeSource
