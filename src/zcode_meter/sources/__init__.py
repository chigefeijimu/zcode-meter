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
# 缺了会打包成功但【该源在 exe 里被静默丢弃】(#15 修订:旧注释宣称
# 『exe 启动即 ImportError』与实测不符 —— pkgutil.iter_modules 只枚举磁盘上
# 实际存在的文件,frozen 包里缺字面导入行的模块根本不进 exe,discovery
# 无从发现它,exe 正常启动、卡片无声缺一个源,dev 环境却一切正常 ——
# 恰是下方 docstring 自认『数据缺失比报错更难被发现』的最坏失败类别)。
# 新增内置源时必须在此补一行,或打包含 --hiddenimport;『字面导入行 ↔
# 包内模块文件』的对应关系由 test_data_engine 的对账断言钉死(任一侧
# 多写/漏写即测试变红,不靠人工记忆)。
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
    数据缺失比报错更难被发现);#38 起形态不合法同样 loudly raise:
    - Source=实例/函数/非 UsageSource 子类的类 → ValueError(README 模板
      一行之差在这里炸出来,而不是静默缺行);
    - 两个不同类同名(#81:引擎把 name 当 per-source 缓存键兼行名,静默
      覆盖=行值张冠李戴、两行同显后源的值)→ ValueError;
    - Source=基类本体再导出(cls is UsageSource)→ 静默豁免:那是接口
      模块的合法写法,不是源声明。
    #16:同一类被再导出(第三方模块写 `from .zcode import Source`)按类
    身份去重 —— 不去重会实例化两次,today_by_source 两行同名同值,卡片
    显示用量翻倍的假象。
    #73:order 取 cls.__dict__ 自身声明,缺省回退 _DEFAULT_ORDER ——
    getattr(cls, ...) 沿 MRO 会把子类化内置源静默继承的 0/10 当成声明,
    击破『未声明 order 的第三方源排在所有内置源之后』。
    #91 注:cls is UsageSource 守卫的必要性不在『无参构造会抛』—— 普通
    类无参构造完全成功、NotImplementedError 延迟到 today_usage() 调用侧
    才抛(引擎每秒现调,#17 守卫兜住);守卫的真正作用是把『接口再导出』
    与『源声明』在发现层分开,漏了守卫的失败形态是『潜伏源行活到运行期
    才被引擎守卫跳过』,而非启动崩溃。
    """
    found = []          # [(order, 模块名, cls)]
    seen_cls = set()    # #16:类身份(再导出去重)
    seen_name = {}      # #81:name -> 首个声明它的模块名
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        m = importlib.import_module(f"{__name__}.{mod.name}")
        cls = getattr(m, "Source", None)
        # 无 Source 属性:非源模块(base 等纯接口/工具模块),跳过不炸
        if cls is None:
            continue
        # 纯接口再导出(基类本体)不算源 —— 静默豁免(见 docstring #91 注)
        if cls is UsageSource:
            continue
        if not (isinstance(cls, type) and issubclass(cls, UsageSource)):
            raise ValueError(
                f"sources.{mod.name}: Source must be a UsageSource subclass "
                f"(got {cls!r}); 卡片静默缺源比启动报错更难被发现")
        if cls in seen_cls:
            continue                    # #16:同一类的再导出,不二次实例化
        seen_cls.add(cls)
        name = getattr(cls, "name", None)
        if isinstance(name, str) and name:
            if name in seen_name:
                raise ValueError(
                    f"sources.{mod.name}: duplicate source name {name!r} "
                    f"(also declared by {seen_name[name]!r}); 同名源在引擎 "
                    "per-source 缓存键/卡片行名上互相覆盖,行值会张冠李戴")
            seen_name[name] = mod.name
        # #73:只认本类 __dict__ 的 order 声明,子类化内置源不继承 0/10
        found.append((cls.__dict__.get("order", _DEFAULT_ORDER), mod.name, cls))
    found.sort(key=lambda t: (t[0], t[1]))
    return [cls() for _order, _name, cls in found]
