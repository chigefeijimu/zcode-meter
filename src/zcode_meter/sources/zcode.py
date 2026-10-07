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
from pathlib import Path

from .base import UsageSource

ZCODE_DIR = os.path.expanduser("~/.zcode/cli")
DB_PATH = os.path.join(ZCODE_DIR, "db", "db.sqlite")

# 历史聚合防御上限:历史聚合查询(fetch_daily_usage / fetch_daily_usage_cost /
# fetch_billing_blocks / fetch_session_usage 与本源 daily_usage)只统计最近
# MAX_SCAN_ROWS 行(rowid 下限),防库无限增长后聚合查询随历史线性变慢。
# 实测库约 3.95 万 completed 行/31 天、日增约 1.3k,100k ≈ 当前 78 天用量,
# 对现有数据零影响;今日轮询(today_usage / 引擎 _poll_stats / Claude 扫描)
# 刻意不设此闸(今日口径是命根子,不做任何窗口裁剪)。超出上限的更早记录
# 不计入历史图表,属预期行为而非『图表变小』bug(README 口径表已加注)。
# 唯一定义在源包侧(#22:data_engine 的四查询与本源 daily_usage 都消费它,
# 而 sources 严禁反向 import data_engine,定义只能放本模块,data_engine 经
# re-import 保旧路径);各消费方在调用时读本常量并以 SQL 占位符参数传入
# (禁字符串内插),单测 monkeypatch 消费方所在命名空间的常量即可调整窗口
# —— 参数绑定是可 patch 性的证据。
MAX_SCAN_ROWS = 100_000


def connect_ro() -> sqlite3.Connection:
    """只读连接:引擎轮询与 UI 侧图表/菜单查询共用,统一 open 参数。
    路径必须经 as_uri() 百分号编码后再拼查询串:原生路径直拼 file: URI 时,
    '#' 会被 SQLite 当 URI fragment 起点 —— 从 '#' 截断到结尾,连 ?mode=ro
    一并卷走,只读失效后在截断出的错误路径静默创建空库文件(写副作用);
    '%' 会被当百分号解码前缀,编码序列即解析失败。用户名/目录含这两个
    字符时打开的是错误的库或打不开,上层 except sqlite3.Error 一律吞成
    0/[](ZCode 源静默清零)。as_uri() 对相对路径抛 ValueError,而测试会
    monkeypatch 相对形态的 DB_PATH,先 abspath 归一(与 sqlite 相对 cwd
    解析的旧行为一致)。"""
    p = Path(DB_PATH)
    if not p.is_absolute():
        p = Path(os.path.abspath(p))
    return sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=2)


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
        # #21/#39:失败自降级返回 0 不 raise(契约 documented limitation,引擎
        # 侧 #17 守卫兜底);con.close 置 finally —— execute 抛错也关连接,
        # 先查后关纪律(fetch_recent_speeds 钉死:close 后再查询会被 except 吞)
        try:
            con = connect_ro()
            try:
                (t,) = con.execute(
                    "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                    " FROM model_usage WHERE status='completed' AND started_at>=?",
                    (today0_ms(),)).fetchone()
            finally:
                con.close()
            return t or 0
        except sqlite3.Error:
            return 0

    def daily_usage(self, days: int = 30) -> list:
        """[(iso日期, tokens)] 升序 —— 契约形状见 base.UsageSource。#22 起补
        MAX_SCAN_ROWS rowid floor(镜像 fetch_daily_usage 同闸):该方法当前全仓
        零调用方(#18 裁决:保留契约对称性),一旦被接线即与历史图表同口径
        同性能闸,不再『接线即分叉』。"""
        try:
            con = connect_ro()
            try:
                rows = con.execute(
                    "SELECT date(started_at/1000, 'unixepoch', 'localtime') AS d,"
                    " COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                    " FROM model_usage WHERE status='completed' AND started_at>=?"
                    " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)"
                    " GROUP BY d ORDER BY d",
                    (today0_ms() - (days - 1) * 86_400_000, MAX_SCAN_ROWS)).fetchall()
            finally:
                con.close()
            return rows
        except sqlite3.Error:
            return []


# discover_sources() 约定:模块级 Source 属性指向本模块的源类(README
# 『如何贡献一个源』)—— 类名本身随意,发现机制只认 Source 这个名字
Source = ZCodeSource
