"""用量源包(Provider 配置化):discover_sources() 自动发现源模块。

约定:包内每个模块(跳过 `_` 前缀)可暴露模块级 `Source` 属性
(UsageSource 子类,必须可无参构造);discover_sources() 按
`(Source.order, 模块名)` 排序实例化 —— 新增 CLI 支持只需加一个模块
文件,零改动 data_engine。源不做运行时热插拔:运行中新增的源文件不会
自动加载,重启生效(frozen 包新增源需重新打包,见 README)。

红线:本包内严禁 import data_engine(见 base.py 模块 docstring)。
"""
from __future__ import annotations

import importlib
import pkgutil

# 字面子模块导入(而非仅按名动态导入):PyInstaller 静态分析只认字面 import,
# 缺了会打包成功但 exe 启动即 ImportError(v0.6.0 --paths 教训)。新增内置源
# 时必须在此补一行,或打包含 --hiddenimport。
from . import base, claude, zcode  # noqa: F401
from .base import UsageSource
from .claude import ClaudeSource
from .zcode import ZCodeSource

__all__ = ["UsageSource", "ZCodeSource", "ClaudeSource", "discover_sources"]

# 未声明 order 的第三方源排在所有内置源之后:内置 ZCode=0 / Claude=10,
# 第三方建议 ≥20(README 贡献指南),留出内置源之间插位的余地
_DEFAULT_ORDER = 100


def discover_sources() -> list:
    """枚举本包源模块并实例化,返回排好序的源实例列表。

    排序键 (Source.order, 模块名):order 小者在前,同 order 按模块名
    稳定排序 —— 保证 ZCode(order=0)恒首位,卡片 today_by_source 的
    显示顺序与迁移前硬编码 [ZCodeSource(), ClaudeSource()] 一致。
    模块导入失败不吞(坏源模块让启动 loudly 失败,好过卡片静默缺一个源:
    数据缺失比报错更难被发现)。
    """
    found = []
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        m = importlib.import_module(f"{__name__}.{mod.name}")
        cls = getattr(m, "Source", None)
        # Source 必须是 UsageSource 的真子类:直接暴露基类本体的模块
        # (接口再导出)不算源 —— 无参实例化即 NotImplementedError
        if (isinstance(cls, type) and issubclass(cls, UsageSource)
                and cls is not UsageSource):
            found.append((getattr(cls, "order", _DEFAULT_ORDER), mod.name, cls))
    found.sort(key=lambda t: (t[0], t[1]))
    return [cls() for _order, _name, cls in found]
