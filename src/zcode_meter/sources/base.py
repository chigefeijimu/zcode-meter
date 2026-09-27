"""用量源接口基类:多 CLI 聚合的唯一契约。

每个源一个子类 + 一个模块(sources/<name>.py 暴露模块级 Source 属性),
由 sources/__init__.py 的 discover_sources() 自动发现 —— 新增 CLI 支持
零改动 data_engine(贡献指南见 README『如何贡献一个源』)。

红线:本包内严禁 import data_engine —— 反向导入会造成循环导入/双模块
实例,data_engine 会被加载成两份、QuotaMonitor 与缓存身份分裂(v0.6.0
src 布局迁移踩过的同类坑,一切常量与连接函数必须单一定义)。
"""
from __future__ import annotations


class UsageSource:
    """用量源接口(v0.4.0 多 CLI 聚合):ZCode 为默认实现,Claude Code 为
    v1 的额外源(本地 jsonl 只读解析)。金额/燃速口径钉死 ZCode-DB-only,
    其他源只进 today_by_source 聚合展示 —— DEFAULT_PRICES 无 Claude 模型,
    计入金额会把 ≈ 永久点亮(评审钉死的口径边界)。"""

    name = "source"

    def is_available(self) -> bool:
        return True

    def today_usage(self) -> int:
        raise NotImplementedError

    def daily_usage(self, days: int = 30) -> list:
        raise NotImplementedError
