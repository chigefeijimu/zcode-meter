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
    """用量源接口(v0.4.0 多 CLI 聚合,Claude Code 源自 v0.4.0 起落地)。
    金额/燃速口径钉死 ZCode-DB-only,其他源只进 today_by_source 聚合展示
    —— DEFAULT_PRICES 无 Claude 模型,计入金额会把 ≈ 永久点亮(评审钉死
    的口径边界)。

    ===== 源契约面(#57:此前计量语义只活在 README 模板注释、order 只在
     sources/__init__.py,『唯一契约』三处分裂 —— 现全部收拢于此)=====

    计量语义:
    - 今日/按天用量的 token 口径 = in + out 之和(in 口径含缓存命中部分,
      与 ZCode 的 input_tokens 已含 cache_read 对齐,勿双计);天界 = 本地
      午夜(today_usage 恒等于 daily_usage 里『今天』那个键的值,UTC 天界
      会把本地 0 点前的用量算进前一天);
    - daily_usage(days) 返回 [(iso日期字符串, 当日 tokens)] 按日期升序,
      缺量的日期不补零(聚合展示消费方按需补);窗口起点=(days-1) 天前
      本地午夜,days=1 只含今天。

    排序与命名:
    - order(类属性,int,缺省 100 = 排在全部内置源之后):discover_sources
      按 (order, 模块名) 排序;注意缺省取的是 cls 自身 __dict__ 的声明,
      子类化内置源不声明自己的 order 会得到 100 而非继承 0/10(#73);
    - name(类属性,str)必须覆写且全局唯一:引擎把 name 当 per-source
      缓存键兼卡片行名(#81 起发现层对同名源 raise);忘覆写会以基类默认
      "source" 字样上卡,同名即发现失败。

    运行时假设(#74:性能正确性依赖,按裸契约实现『口径正确但无缓存』
    的源会落地即每秒全量扫盘,无机制拦截):
    - today_usage 会被引擎线程每秒现调(非 DB 源无缓存路径);实现必须
      自带节流/增量(参照 ClaudeSource 的 SCAN_TTL + 增量偏移),单次
      调用成本须与『上次调用以来的新增量』同阶,而非与历史总量同阶。

    失败语义(#39 documented limitation,签名保持 int 不变):
    - is_available/today_usage/daily_usage 失败时【自降级】返回 False/0/
      [],不得 raise —— 契约没有错误通道(错误通道 API 化属范围蔓延,
      评审否决);引擎侧对 per-source 调用另有 try/except Exception 守卫
      (#17,第三方源抛异常该源当拍跳过)兜底,源实现不应依赖该守卫
      当正常路径。
    """

    name = "source"

    def is_available(self) -> bool:
        return True

    def today_usage(self) -> int:
        raise NotImplementedError

    def daily_usage(self, days: int = 30) -> list:
        raise NotImplementedError
