"""zcode-meter 数据层:日志 tail + SQLite 轮询 + 流式估算(UI 无关,tk/Qt 共用)。

v0.4.0 起另含:价格表/金额估算、多用量源(Claude 只读解析)、quota 刷新线程、
预算告警状态机、5h 计费块聚合 —— 但 QuotaMonitor 只由 zcode_meter_qt.
MeterWindow 实例化(见各类 docstring 的启动位置钉死说明)。
v0.5.1 起 quota 由 300s 盲轮询改为事件驱动+节流(见 quota_fetch_decision)。
用量源已迁 sources/ 包(Provider 配置化):UsageSource/ZCodeSource/ClaudeSource
与 ZCODE_DIR/DB_PATH/connect_ro/today0_ms 的唯一定义都在那边,本模块 re-import
保住全部旧导入路径(见下方 import 块),源行为零变化。
"""
from __future__ import annotations

import calendar
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import faulthandler
import json
import os
import queue
import sqlite3
from collections import deque
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field

# ---- 用量源包(sources/,Provider 配置化)----
# UsageSource/ZCodeSource/ClaudeSource 与 ZCODE_DIR/DB_PATH/connect_ro/today0_ms
# 的唯一定义都在 sources/(base.py/zcode.py/claude.py);此处模块级 re-import 是
# 兼容契约:tests 与外部脚本沿用 from zcode_meter.data_engine import DB_PATH /
# de.today0_ms() 等全部旧导入路径,一个都不能断(test_package 的 hasattr 同理)。
# sources 包内严禁反向 import data_engine —— 循环导入会让本模块加载成两个
# 实例、QuotaMonitor 与缓存身份分裂(v0.6.0 双路径 import 红线)。
from .sources import (UsageSource, ZCodeSource,  # noqa: F401  纯 re-export
                      ClaudeSource, discover_sources)
from .sources.zcode import (ZCODE_DIR, DB_PATH,  # noqa: F401  DB_PATH 为纯 re-export
                            connect_ro, today0_ms)


def app_dir() -> str:
    """运行目录:PyInstaller frozen 时取 exe 所在目录(zm_*.log/zm_state.json
    都落这);脚本模式(src 布局,包在 src/zcode_meter/ 下)向上找含 README.md
    的祖先目录 = 仓库根。**这是路径不变式**:v0.5.x 及以前扁平布局时 CONFIG_PATH/
    PRICES_PATH/ALERTS_PATH/_CRASH_LOG/DBG_PATH 与 UI 侧 STATE_PATH 全落仓库根,
    若改成包目录,用户现有 zm_config.json(含 quota key)会无声失配 ——
    load_config 吞 OSError 静默回退默认,属最隐蔽的回归,故以仓库根为锚。
    裸拷贝包目录等找不到锚点时回退包目录(行为等价旧扁平布局)。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    for _ in range(3):                     # src/zcode_meter 距仓库根两级,留裕量;防越出仓库无限爬
        d = os.path.dirname(d)
        if d == os.path.dirname(d):        # 已到盘根仍未命中锚点
            break
        if os.path.isfile(os.path.join(d, "README.md")):
            return d
    return here


# 崩溃追踪:pythonw 无控制台,access violation 等原生崩溃的 traceback 落盘。
# 打不开不能连启动都崩:exe 可能被放进只读目录(如未提权的 Program Files)
_CRASH_LOG = os.path.join(app_dir(), "zm_crash.log")
try:
    faulthandler.enable(open(_CRASH_LOG, "a", encoding="utf-8"))
except Exception:
    pass

# ZCODE_DIR/DB_PATH/connect_ro/today0_ms 的唯一定义已下沉 sources/zcode.py
# (顶部 re-import);LOG_DIR/ROLL_DIR 就地派生 —— 日志 tail 与回退会话识别
# 仍是 data_engine 职责,不随只读连接下沉源包。
LOG_DIR = os.path.join(ZCODE_DIR, "log")
ROLL_DIR = os.path.join(ZCODE_DIR, "rollout")

DEBUG = os.environ.get("ZM_DEBUG") == "1"
DBG_PATH = os.path.join(app_dir(), "zm_debug.log")


def dbg(msg: str):
    if DEBUG:
        with open(DBG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} {msg}\n")


# 历史聚合防御上限:历史图表四查询(fetch_daily_usage / fetch_daily_usage_cost /
# fetch_billing_blocks / fetch_session_usage)只统计最近 MAX_SCAN_ROWS 行
# (rowid 下限),防库无限增长后聚合查询随历史线性变慢。实测库约 3.95 万
# completed 行/31 天、日增约 1.3k,100k ≈ 当前 78 天用量,对现有数据零影响;
# 今日轮询(_poll_stats / ZCodeSource.today_usage / Claude 扫描)刻意不设此闸
# (今日口径是命根子,不做任何窗口裁剪)。超出上限的更早记录不计入历史图表,
# 属预期行为而非『图表变小』bug(README 口径表已加注)。四函数在调用时读本
# 常量并以 SQL 占位符参数传入(禁字符串内插),单测 monkeypatch 本常量即可
# 调整窗口 —— 参数绑定是可 patch 性的证据。
MAX_SCAN_ROWS = 100_000


# ---------------------------------------------------- 配置/价格/守卫(v0.4.0) ---

CONFIG_PATH = os.path.join(app_dir(), "zm_config.json")

# 贴边条可勾选段(v0.8.0 对版期,用户『靠边停放时自定义显示内容』):
# 横条(顶/底共用)与竖条(左/右共用)各一套;速度数字+圆点恒显不进清单。
BAR_SEGMENTS_H = ("spark", "plan", "cd", "today", "burn")
BAR_SEGMENTS_V = ("spark", "today", "plan", "burn", "in")
# 菜单展示名(勾选项 label;键与上面元组一一对应)
BAR_SEGMENT_LABELS_H = {"spark": "速度曲线", "plan": "套餐余量", "cd": "重置倒计时",
                        "today": "今日用量+金额", "burn": "燃速·均燃"}
BAR_SEGMENT_LABELS_V = {"spark": "速度曲线", "today": "今日消耗", "plan": "套餐余量",
                        "burn": "燃速·均燃", "in": "会话入出"}


def _norm_bar_segments(obj) -> dict:
    """bar_segments 规范化:{"h":[...], "v":[...]},白名单交集保序;键缺失/
    形状不对 → 该形态回退全开(默认=现状,存量配置文件零迁移)。"""
    out = {}
    for form, allowed in (("h", BAR_SEGMENTS_H), ("v", BAR_SEGMENTS_V)):
        out[form] = list(allowed)          # 缺省全开
    if not isinstance(obj, dict):
        return out
    for form, allowed in (("h", BAR_SEGMENTS_H), ("v", BAR_SEGMENTS_V)):
        lst = obj.get(form)
        if isinstance(lst, list):
            keep = [s for s in allowed if s in lst]
            if keep or lst == []:
                out[form] = keep           # 空列表合法(纯速度胶囊)
    return out
PRICES_PATH = os.path.join(app_dir(), "zm_prices.json")
ALERTS_PATH = os.path.join(app_dir(), "zm_alerts.json")


def _no_persist() -> bool:
    """数据层自备的状态守卫:与 zcode_meter_qt._state_guard 同语义,但刻意
    不 import UI 模块(反向依赖会让数据层测试拖起整个 Qt)。为真时:
    QuotaMonitor 不启动、zm_alerts.json 不写 —— 回归测试(--verify 自检或
    ZM_NO_STATE=1 注入)绝不发真实网络请求、绝不出测试污染文件。"""
    return os.environ.get("ZM_NO_STATE") == "1" or "--verify" in sys.argv


def _is_num(x) -> bool:
    """宽松数值判定:bool 是 int 子类,须显式排除(价格/阈值里 true 是坏值)。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _norm_quota_refresh(v):
    """quota_refresh(设置窗『quota 刷新间隔』档位)合法性判定与规范化:
    合法值 = "auto"(事件驱动+节流,缺省语义)或 int∈[60,86400] 秒的固定
    间隔轮询 —— 下限 60 与 QUOTA_MIN_GAP 硬闸同义(用户显式选的间隔不得
    比 auto 档硬闸更密),上限 24h 防手改文件写出『一天只查一次』的死值。
    bool 是 int 子类须显式排除(同 _is_num 口径)。返回规范化值("auto" 或
    int),非法 → None。
    可选键语义(保 test_save_config_roundtrip 三键全等断言的关键裁决):
    load_config 仅当文件含合法值才在返回 dict 放该键、save_config 仅当
    输入含合法值才写该键,缺省/非法 → 整键省略;消费方一律
    cfg.get("quota_refresh", "auto") —— 第 4 键只在用户显式选了固定档时
    出现,旧配置文件与既有断言零感知。"""
    if v == "auto":
        return "auto"
    if isinstance(v, int) and not isinstance(v, bool) and 60 <= v <= 86400:
        return v
    return None


def load_config() -> dict:
    """zm_config.json(用户本地文件,已 .gitignore,防 key 随仓库提交):
    quota_api_key(str)、daily_budget_cny(>0 数字)、alert_pct(正数列表)、
    quota_refresh(可选:仅文件含合法值时含键,见 _norm_quota_refresh)。
    缺文件/坏 JSON/字段类型不对一律回退默认,不抛错。任何日志与调试路径
    都不得打印 key 明文(泄漏面专查项)。"""
    cfg = {"quota_api_key": "", "daily_budget_cny": None, "alert_pct": [20.0, 10.0]}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return cfg
    if not isinstance(obj, dict):
        return cfg
    key = obj.get("quota_api_key")
    if isinstance(key, str) and key.strip():
        cfg["quota_api_key"] = key.strip()
    budget = obj.get("daily_budget_cny")
    if _is_num(budget) and budget > 0:
        cfg["daily_budget_cny"] = float(budget)
    pcts = obj.get("alert_pct", obj.get("alert_thresholds"))
    if isinstance(pcts, list):
        vals = sorted({float(v) for v in pcts if _is_num(v) and v > 0}, reverse=True)
        if vals:
            cfg["alert_pct"] = vals
    # 可选键:仅文件含合法档位才放键(缺文件/坏 JSON 的早退路径天然无此键)
    rv = _norm_quota_refresh(obj.get("quota_refresh"))
    if rv is not None:
        cfg["quota_refresh"] = rv
    # 可选键:贴边条可勾选段(v0.8.0 对版期;缺省全开=存量文件零迁移)
    cfg["bar_segments"] = _norm_bar_segments(obj.get("bar_segments"))
    return cfg


def save_config(cfg: dict, path: str | None = None) -> bool:
    """zm_config.json 落盘(v0.5.0 设置窗路径):与 load_config 完全同规则
    规范化(key strip / budget>0 或 None / pct 正数降序去重,坏值回退默认),
    临时文件 + os.replace 原子替换 —— 直接 open('w') 中途崩溃会坏掉配置,
    而 load_config 对坏文件静默回退默认,key 会无声丢失(最敏感字段,必须
    原子写)。失败语义是返回 bool、绝不抛错:
    - path 缺省(默认 CONFIG_PATH)且 _no_persist() 为真 → 返回 False 且不写:
      ZM_NO_STATE=1 环境残留(README 故障排查表)下设置窗仍可打开,若这里
      返回 True,UI 会照常热生效并关闭对话框,而 key 从未落盘、重启即无声
      丢失(『用户以为改了实际没改』红线);显式 path 供单测绕过守卫。
    - OSError(exe 放只读目录等)→ 清理残留临时文件后返回 False,调用方
      (设置窗状态行)负责向用户报错,这里既不抛也不静默当成功。
    全程不打 key 明文日志(泄漏面专查项)。"""
    p = path if path is not None else CONFIG_PATH
    if path is None and _no_persist():
        return False
    # 规范化与 load_config 同规则:保证任何来源(含 UI 输入解析外的直接调用)
    # 落盘形状恒合法;load_config 读回同一份数据必然得到相同结果
    key = cfg.get("quota_api_key")
    out = {"quota_api_key": key.strip() if isinstance(key, str) else "",
           "daily_budget_cny": None, "alert_pct": [20.0, 10.0]}
    budget = cfg.get("daily_budget_cny")
    if _is_num(budget) and budget > 0:
        out["daily_budget_cny"] = float(budget)
    pcts = cfg.get("alert_pct")
    if isinstance(pcts, list):
        vals = sorted({float(v) for v in pcts if _is_num(v) and v > 0}, reverse=True)
        if vals:
            out["alert_pct"] = vals
    # 可选键:仅输入含合法档位才写第 4 键(缺省/非法整键省略)—— 无条件
    # 写会把三键 cfg 也变成四键文件,翻掉 test_save_config_roundtrip 的
    # back2 三键全等断言,且旧配置文件平白多一个无信息量的默认键
    rv = _norm_quota_refresh(cfg.get("quota_refresh"))
    if rv is not None:
        out["quota_refresh"] = rv
    # 可选键:贴边条可勾选段 —— 仅当调用方显式携带才写(同上第 4 键纪律:
    # 三键 cfg 直存不升四键);写前规范化,形状恒合法
    segs = cfg.get("bar_segments")
    if segs is not None:
        out["bar_segments"] = _norm_bar_segments(segs)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, p)
    except OSError:
        # 两条失败路径都在这收口:临时文件创建失败(目录不存在/只读)与
        # replace 失败(Windows 下目标被占用抛 PermissionError⊂OSError)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


# bigmodel 按量刊例价(元/M tokens),2026-09 自官方定价文档人工转录:
# https://docs.bigmodel.cn/cn/guide/start/pricing
# - GLM-5.3-Flash 取标准牌价(限时 5 折期实付更低 → 宁可高估,可自行用
#   zm_prices.json 调低);免费模型三档全 0。
# - 官方按上下文长度分档计费的模型(GLM-5.1/5-Turbo/5/4.7/4.5-Air)静态表
#   只能取一档,统一取最高档保守估算(同"宁可高估"原则),同样可覆盖。
# - 非 bigmodel 模型(deepseek-* 等)不在此表 → 未知模型口径:计 ¥0 + partial。
DEFAULT_PRICES: dict = {
    "GLM-5.3":           {"in": 8.0, "in_cache": 2.0,   "out": 28.0},
    "GLM-5.3-Flash":     {"in": 0.8, "in_cache": 0.23,  "out": 2.8},
    "GLM-5.3-FlashX":    {"in": 2.0, "in_cache": 0.57,  "out": 7.0},
    "GLM-5.2":           {"in": 8.0, "in_cache": 2.0,   "out": 28.0},
    # ↓ 分档计费模型,取最高档(保守)
    "GLM-5.1":           {"in": 8.0, "in_cache": 2.0,   "out": 28.0},
    "GLM-5-Turbo":       {"in": 7.0, "in_cache": 1.8,   "out": 26.0},
    "GLM-5":             {"in": 6.0, "in_cache": 1.5,   "out": 22.0},
    "GLM-4.7":           {"in": 4.0, "in_cache": 0.8,   "out": 16.0},
    "GLM-4.5-Air":       {"in": 1.2, "in_cache": 0.24,  "out": 8.0},
    "GLM-4.7-FlashX":    {"in": 0.5, "in_cache": 0.1,   "out": 3.0},
    "GLM-4.7-Flash":     {"in": 0.0, "in_cache": 0.0,   "out": 0.0},   # 免费
    "GLM-4-Plus":        {"in": 5.0, "in_cache": 2.5,   "out": 5.0},
    "GLM-4-Air-250414":  {"in": 0.5, "in_cache": 0.25,  "out": 0.5},
    "GLM-4-Long":        {"in": 1.0, "in_cache": 0.5,   "out": 1.0},
    "GLM-Z1-Air":        {"in": 0.5, "in_cache": 0.5,   "out": 0.5},
    "GLM-Z1-AirX":       {"in": 5.0, "in_cache": 5.0,   "out": 5.0},
    "GLM-Z1-FlashX":     {"in": 0.1, "in_cache": 0.1,   "out": 0.1},
    "GLM-4-FlashX-250414": {"in": 0.1, "in_cache": 0.05, "out": 0.1},
}


def load_prices() -> dict:
    """DEFAULT_PRICES 深拷贝 + zm_prices.json 模型级 merge。
    坏文件(缺文件/坏 JSON/非 dict)整体忽略回退默认;单个模型条目非 dict、
    或缺 in/out 必备档、或档位值非数值 → 跳过该条目(不污染默认表)。
    对默认表内已有模型支持只覆盖给出的档位(如只改 in_cache)。"""
    prices = {m: dict(t) for m, t in DEFAULT_PRICES.items()}
    try:
        with open(PRICES_PATH, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return prices
    if not isinstance(obj, dict):
        return prices
    for model, tiers in obj.items():
        if not isinstance(model, str) or not isinstance(tiers, dict):
            continue
        merged = dict(prices.get(model, {}))
        for k in ("in", "in_cache", "out"):
            if k in tiers and _is_num(tiers[k]):
                merged[k] = float(tiers[k])
        if _is_num(merged.get("in")) and _is_num(merged.get("out")):
            prices[model] = merged
    return prices


def cost_of(prices: dict, model: str, in_tok: int, out_tok: int,
            cache_read: int) -> tuple:
    """一组用量的按刊例价金额(元)。两条口径钉死(混用会静默错价一个
    数量级 —— cache_read 实测占 input ~98%,单测是唯一防线):
    1. 已知模型缺 in_cache 档 → cache_read 按 in 全价计(宁可高估,不置 partial);
    2. 未知模型(in 或 out 档缺失同理)→ 计 ¥0 并置 partial(UI 显示 ≈)。
    注意:ZCode 的 input_tokens 已含 cache_read,未命中部分 = in - cache_read,
    切勿再叠加。返回 (cny, partial)。"""
    p = prices.get(model) if isinstance(prices, dict) else None
    if not isinstance(p, dict):
        return 0.0, True
    p_in, p_out = p.get("in"), p.get("out")
    if not _is_num(p_in) or not _is_num(p_out):
        return 0.0, True
    p_cache = p.get("in_cache")
    if not _is_num(p_cache):
        p_cache = p_in                     # 规则 1:缺缓存档按 in 全价
    in_tok, out_tok, cache_read = in_tok or 0, out_tok or 0, max(cache_read or 0, 0)
    miss = max(in_tok - cache_read, 0)     # 数据异常(cache>in)时钳到 0,不产负价
    cny = (miss * p_in + cache_read * p_cache + out_tok * p_out) / 1_000_000
    return cny, False


def est_hours_left(daily_budget_cny, today_cost_cny, burn_cny_per_hour):
    """按当前燃速,日预算还能撑几小时 = (预算-今日花费)/燃速¥/h。
    燃速为 0(除零)或未配日预算时返回 None;负值透传(UI 显示"已超支")。"""
    if not daily_budget_cny or not burn_cny_per_hour or burn_cny_per_hour <= 0:
        return None
    return (daily_budget_cny - (today_cost_cny or 0)) / burn_cny_per_hour


def trend_forecast(model_rows, today=None) -> "dict | None":
    """近 7 个日历日的趋势外推(纯函数,历史图表『本月预计』的数据源)。

    model_rows = fetch_daily_model_usage 的行形状 (date, model, in_tok,
    cache, out_tok, cny, partial);本函数只读 date / in_tok+out_tok / cny /
    partial 四个字段(in_tok 已含 cache_read 勿再加,同今日口径),不触库,
    可用合成行离线单测。返回 {avg_tokens, avg_cny, forecast_cny, partial}
    或 None,口径钉死(与 UI 两处展示互为对账依据):
    - 窗口 = 以 today 为末日(含)往前连续 7 个日历日,缺量日补零(padding
      与历史图表 30 天补齐同口径 —— 补零让『整周没用』显式摊薄日均,而不是
      把均值偷偷抬到只算有数据的那几天);窗口外的旧行直接忽略(防御)。
    - avg 恒除 7(窗口宽),不按有数据的天数除:用量集中在少数天时按实际
      天数除会高估日均、进而高估月度预计。
    - forecast_cny = avg_cny × 当月总天数(calendar.monthrange)—— 裁决采
      『近 7 天日均 × 当月天数』直推;月初样本尚少时外推粗暴,属已知取舍。
    - 窗口总 token 用量 0(空库/近 7 天零用量)→ None,宁缺勿错:0 日均
      外推出的『本月预计 ¥0.00』毫无信息量,徒占图表标注位。
    - partial = 窗口内任一行含未知模型(¥ 为下限,UI 在 ¥ 前加 ≈)。
    舍入约定:avg_tokens 取 1 位小数(fmt_k 直接可显示)、avg_cny/forecast
    取 4 位(与 fetch_daily_usage_cost 同精度);forecast 用舍入后的
    avg_cny 计算,保证 UI 上看到的两个数自洽(forecast == avg × 当月天数)。
    today 缺省取本地今日;显式传 dt.date 供单测钉死窗口与当月天数。"""
    end = dt.date.today() if today is None else today
    if isinstance(end, dt.datetime):        # 宽容:datetime 也认,取其日期部分
        end = end.date()
    keys = {(end - dt.timedelta(days=k)).isoformat() for k in range(7)}
    tot_tok, tot_cny, partial = 0, 0.0, False
    for row in model_rows or ():
        if str(row[0]) not in keys:
            continue
        tot_tok += int(row[2] or 0) + int(row[4] or 0)
        tot_cny += float(row[5] or 0)
        partial = partial or bool(row[6])
    if tot_tok <= 0:
        return None
    avg_cny = round(tot_cny / 7, 4)
    return {"avg_tokens": round(tot_tok / 7, 1),
            "avg_cny": avg_cny,
            "forecast_cny": round(
                avg_cny * calendar.monthrange(end.year, end.month)[1], 4),
            "partial": partial}


# ------------------------------------------------------ 预算告警(双轨共芯) ---

class BudgetAlerts:
    """双轨(quota/按量)预算告警的纯状态机,UI 线程调用:
    - 级别 = 剩余百分比阈值(默认 [20,10] 降序):跌破某级别当日提醒一次;
    - 同级别同日只提醒一次(不重复骚扰),跨日自动重置(日期变了自然失效);
    - 一轮内把所有已跌入且未提醒的级别都标记,只返回最深的那个(浅级别
      属旧闻不再补报);回升不重置已提醒标记(防反复横跳刷屏)。
    状态持久化到独立的 zm_alerts.json —— zm_state.json 的 _load_state 有
    严格键类型校验,混入会破坏位置记忆;守卫环境下不写防测试污染。"""

    def __init__(self, thresholds=(20.0, 10.0), state_path: str | None = None):
        self.thresholds = sorted({float(t) for t in thresholds if t > 0}, reverse=True)
        self.state_path = state_path or ALERTS_PATH
        self._fired: dict = {}            # f"{track}|{level}" -> 提醒日 YYYY-MM-DD
        self._load()

    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict) and isinstance(obj.get("fired"), dict):
                self._fired = {str(k): str(v) for k, v in obj["fired"].items()}
        except (OSError, json.JSONDecodeError, ValueError):
            self._fired = {}

    def _save(self):
        if _no_persist():
            return
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"fired": self._fired}, f, ensure_ascii=False)
        except OSError:
            pass                    # 状态保存失败不影响运行(同 zm_state 语义)

    def evaluate(self, track: str, remaining_pct, today: str):
        """评估某轨剩余百分比:有新命中的级别返回最深的级别值,否则 None。
        track 仅作状态隔离键("quota"/"budget"),today 由调用方传入便于测试。"""
        if remaining_pct is None or not self.thresholds:
            return None
        deepest_new = None
        for lv in self.thresholds:                # 降序:浅 → 深,循环结束停在最深
            if remaining_pct > lv:
                continue
            key = f"{track}|{lv:g}"
            if self._fired.get(key) != today:
                self._fired[key] = today
                deepest_new = lv
        if deepest_new is not None:
            self._save()
        return deepest_new

    def fire_once(self, key: str, today: str) -> bool:
        """当日一次性事件去重(quota 重置通知等,非预算阈值事件):key 当日
        首次 → True 并落盘;同日再来 → False;跨日自然重置(与 evaluate 同
        语义)。刻意复用 _fired/_save/_load 而非另开状态文件 —— zm_alerts.json
        的 {"fired": {key: 日期}} 形状不变,守卫语义同 _save(守卫环境不写盘,
        内存去重仍成立)。键名由调用方保证不与 evaluate 的 f"{track}|{lv:g}"
        冲突(重置通知用 "reset_5h"/"reset_cycle",track 名空间不同)。"""
        k = str(key)
        if self._fired.get(k) == today:
            return False
        self._fired[k] = today
        self._save()
        return True


# ------------------------------------------------ quota 轨(Coding Plan 余量) ---

QUOTA_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"


def _as_number(x):
    """quota 载荷的字段兼容数字与字符串双形态(社区逆向接口,两形态都见过)。"""
    if _is_num(x):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.strip())
        except ValueError:
            return None
    return None


def _quota_sanity_dbg(best: dict, cyc: dict | None):
    """parse 的二重校验,**仅 dbg 记录、绝不改取数**:5h 窗的 nextResetTime
    距 now 不在 (0,5h]、或长周期窗的 reset 距 now ≤5h 时各记一条 —— 社区
    逆向接口结构漂移的第一现场线索,便于下次修 parse;ZM_DEBUG 未开时
    dbg 零开销,不影响正常路径。"""
    now_ms = time.time() * 1000
    nrt = best.get("next_reset_ms")
    if nrt is not None and not (0 < nrt - now_ms <= 18_000_000):
        dbg(f"quota sanity: 5h nextResetTime 距 now {nrt - now_ms:.0f}ms "
            f"不在 (0,5h],结构疑似漂移")
    if cyc is not None:
        cnrt = cyc.get("next_reset_ms")
        if cnrt is not None and cnrt - now_ms <= 18_000_000:
            dbg(f"quota sanity: cycle(number={cyc['number']:g}) reset 距 now "
                f"{cnrt - now_ms:.0f}ms ≤5h,疑似非长周期窗")


def parse_quota_payload(obj) -> dict | None:
    """quota 接口载荷 → {'window_hours':5, 'used_pct', 'remaining_pct',
    'next_reset_ms'}(4 键契约不变:既有单测与 UI 只读这 4 键)。结构(2026-09
    实测逆向,非官方文档):data.limits[].type∈{TOKENS_LIMIT,CREDIT_LIMIT,
    MCP_LIMIT,TIME_LIMIT};percentage 为**已用**百分比 → remaining =
    100 - percentage;在 TOKENS_LIMIT 里选 number==5 的条目作 5h 计费窗,
    并透出 nextResetTime(ms)。
    新增可选键 cycle(重置通知判据用):TOKENS_LIMIT 且 number!=5 中 number
    最小条目(长周期 token 计费窗,实测样例 number:30)的 {'number',
    'used_pct', 'remaining_pct', 'next_reset_ms'};无候选则整键省略,
    消费方一律 .get('cycle')。刻意不把 CREDIT_LIMIT 等其他 type 当长周期:
    钱/额度轨的重置语义与 token 计费窗不同,混入会把额度变动误报成 token
    窗重置(偏离 ROADMAP『其他 type → weekly/月度』草案的原因)。
    结构对不上/没有 5h 窗 → None(该轨优雅降级为不显示,绝不抛错)。"""
    try:
        limits = obj["data"]["limits"]
        if not isinstance(limits, list):
            return None
        best = None
        cyc = None
        for it in limits:
            if not isinstance(it, dict) or it.get("type") != "TOKENS_LIMIT":
                continue
            num = _as_number(it.get("number"))
            pct = _as_number(it.get("percentage"))
            if num == 5 and pct is not None:
                nrt = _as_number(it.get("nextResetTime"))
                best = {"window_hours": 5.0,
                        "used_pct": pct,
                        "remaining_pct": 100.0 - pct,
                        "next_reset_ms": int(nrt) if nrt is not None else None}
            elif (num is not None and num != 5 and pct is not None
                  and (cyc is None or num < cyc["number"])):
                # 长周期窗候选:取 number 最小(同 number 多条时保留首条,
                # 社区样例未见此形态,不值得额外裁决)
                nrt = _as_number(it.get("nextResetTime"))
                cyc = {"number": num,
                       "used_pct": pct,
                       "remaining_pct": 100.0 - pct,
                       "next_reset_ms": int(nrt) if nrt is not None else None}
        if best is None:
            return None            # 没有 5h 窗:整轨降级,cycle 也无从谈起
        if cyc is not None:
            best["cycle"] = cyc    # 可选键语义:仅值合法时含键
        _quota_sanity_dbg(best, cyc)
        return best
    except (KeyError, TypeError, ValueError):
        return None


def quota_reset_event(prev: dict | None, cur: dict | None,
                      now_ms: float | None = None) -> str | None:
    """两次 quota 快照之间是否发生额度重置、重置的是哪类窗(纯函数,UI 线程
    逐快照调用):'5h' | 'cycle' | None。
    主判据 = nextResetTime 前跳:正常倒计时只会递减,cur 比 prev 前跳超过
    60s(5h 与 cycle 两窗各自比)即判该窗刚重置;60s 容差滤掉接口侧小幅
    重排/时钟抖动(恰 +60s 不算,严格大于才算)。
    回退判据(仅 5h 窗、仅当缺 nextResetTime):remaining_pct 跳升 Δ≥+30 个
    百分点且新值>50 → '5h'。cycle 不做回退:长周期窗的跳升形态未实测,
    宁缺勿错。有 reset 时刻时主判据独裁 —— remaining 本身随消耗波动,
    叠加第二触发源会放大误报。
    同一 tick 两窗同时命中 → 'cycle'(更罕见,优先报)。
    prev 为 None(首查)→ None:首查不触发是硬约定,否则换号/首配 key 的
    第一次查询必误报『已重置』。now_ms 仅供 dbg 上下文,不参与判定。"""
    if not isinstance(prev, dict) or not isinstance(cur, dict):
        return None

    def _jumped(p_win, c_win) -> bool:
        """该窗 nextResetTime 是否前跳 >60s:两侧都有有效值才可比(任一
        缺失/非法即不可比 → False,首见新窗不触发)。"""
        if not isinstance(p_win, dict) or not isinstance(c_win, dict):
            return False
        pn, cn = p_win.get("next_reset_ms"), c_win.get("next_reset_ms")
        if not _is_num(pn) or not _is_num(cn):
            return False
        return cn > pn + 60_000

    ev_5h = _jumped(prev, cur)
    ev_cyc = _jumped(prev.get("cycle"), cur.get("cycle"))
    if ev_5h or ev_cyc:
        ev = "cycle" if ev_cyc else "5h"     # 双窗同拍归 cycle
        if _is_num(now_ms):
            dbg(f"quota reset event: {ev} (now_ms={now_ms})")
        return ev
    # 回退判据:仅 5h 窗、仅当缺 nextResetTime(两侧任一缺失即『缺』)
    pn, cn = prev.get("next_reset_ms"), cur.get("next_reset_ms")
    if not _is_num(pn) or not _is_num(cn):
        pr, cr = prev.get("remaining_pct"), cur.get("remaining_pct")
        if _is_num(pr) and _is_num(cr) and cr - pr >= 30.0 and cr > 50.0:
            return "5h"
    return None


# ---- v0.5.1 事件驱动+节流:何时真发 quota 请求,判定抽成纯函数便于单测 ----
# 三道闸(倒计时本地化对标 GLM Monitor 的已验证做法,节流档位以需求原文为准):
# 60s 最小间隔硬闸 / 活跃期(过去 1h 内有请求)常态最长 3min 一查 / 静默期完全暂停
QUOTA_MIN_GAP = 60.0            # 频率下限(硬闸,任何触发类型都不得快于它)
QUOTA_ACTIVE_GAP = 180.0        # 活跃期常态间隔
QUOTA_ACTIVE_WINDOW = 3600.0    # 活跃判定窗口(s):窗口内有请求才算活跃


def quota_fetch_decision(now: float, last_fetch: float | None,
                         last_activity: float | None, force: bool = False,
                         mode: str | int = "auto", gap: float = 0.0) -> bool:
    """本次 tick 是否真的向 quota 接口发请求(纯函数,无副作用,单测钉死)。
    判定顺序(闸门链,任一闸命中即拒):
    ① force → 放行:启动首查/设置窗换 key 重启,对应『立即查一次』;
    ② last_fetch 为 None → 放行:兜底(首轮记账前未知上次抓取时刻);
    ③ now−last_fetch < QUOTA_MIN_GAP → 拒:60s 硬闸。常规路径不可达
      (活跃被⑤的 180s 常态闸完全覆盖、静默被④拦截),作为防御性硬闸与
      未来触发类型(手动刷新、新触发源)的频率下限保留;
    ④ last_activity 为 None 或 now−last_activity > QUOTA_ACTIVE_WINDOW → 拒:
      静默期(>1h 无任何请求)完全暂停;恰好 3600s 仍算活跃(边界归活跃);
    ⑤ now−last_fetch < QUOTA_ACTIVE_GAP → 拒:活跃期常态 ≤1 次/3min;
    ⑥ 放行。
    手动档(mode != "auto",设置窗『quota 刷新间隔』选了固定间隔,QuotaMonitor
    以 refresh=间隔秒数调用):闸门链 ③④⑤ 全部不适用 —— 静默期也照查、
    与活动信号无关,这是用户显式选择的语义;间隔本身经 _norm_quota_refresh
    钳在 [60,86400],60s 硬闸语义已由配置层满足,故函数内不再重复设闸。
    判定 = force 或 last_fetch 为 None 或 now−last_fetch ≥ gap(恰好到点
    放行,与⑤『< 才拒』同侧边界);gap 缺省 0 仅是占位,手动档调用方
    必须显式传间隔。mode/gap 均为默认参数 → 既有调用(全位置参数或
    force= 形态)行为逐字不变。"""
    if mode != "auto":
        return bool(force or last_fetch is None or now - last_fetch >= gap)
    if force:
        return True
    if last_fetch is None:
        return True
    if now - last_fetch < QUOTA_MIN_GAP:
        return False
    if last_activity is None or now - last_activity > QUOTA_ACTIVE_WINDOW:
        return False
    if now - last_fetch < QUOTA_ACTIVE_GAP:
        return False
    return True


def format_countdown_hm(next_reset_ms, now_ms=None) -> str | None:
    """重置倒计时本地化文案(对标 GLM Monitor:倒计时纯本地每分钟递减,
    不为它发任何 API 请求):'1h 23m'/'42m'。分钟向上取整(剩 30s 显示
    1m,不闪 '0m');已重置(≤0)或参数缺失/非法 → None,UI 自然省略该段。"""
    if not _is_num(next_reset_ms) or next_reset_ms <= 0:
        return None
    if now_ms is not None and not _is_num(now_ms):
        return None
    now = time.time() * 1000 if now_ms is None else float(now_ms)
    rem_s = (float(next_reset_ms) - now) / 1000.0
    if rem_s <= 0:
        return None
    mins = int(-(-rem_s // 60))           # ceil 整数化:30.4s → 1m、90.5s → 2m
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def format_age_zh(ts, now=None) -> str | None:
    """数据新鲜度文案(『套餐剩余 N% · 3分钟前』的尾巴),让用户知道数据
    多新:ts 为查询完成时刻(time.time() 秒)。<60s '刚刚',<60min
    'N分钟前',否则 'N小时前'。ts 缺失/非法/在未来(时钟回拨)→ None,
    UI 自然省略,宁缺勿错。纯本地计算。"""
    if not _is_num(ts) or ts <= 0:
        return None
    if now is not None and not _is_num(now):
        return None
    ref = time.time() if now is None else float(now)
    age = ref - ts
    if age < 0:
        return None
    if age < 60.0:
        return "刚刚"
    if age < 3600.0:
        return f"{int(age // 60)}分钟前"
    return f"{int(age // 3600)}小时前"


class QuotaMonitor(threading.Thread):
    """Coding Plan 套餐余量刷新(daemon 线程):GET quota/limit(只读,全程
    唯一允许的外部请求端点)。urllib 标准库实现 —— 不引第三方依赖,保住
    exe 打包体积。线程只产出普通 dict,Qt 调用全部留在 UI 线程(评审约定)。

    调度(v0.5.1 事件驱动+节流,替代旧 300s 盲轮询):1s tick 检查
    quota_fetch_decision —— DataEngine 监测到新 completed 请求(『有消耗』)
    经 notify_activity 报告活跃;活跃期常态最长 3min 一查,静默期(>1h 无
    请求)完全暂停,60s 硬闸兜底;首轮 force=启动即查(换 key 是新实例,
    同样立即查)。重置倒计时由 UI 按本地时钟对 next_reset_ms 递减渲染,
    不为倒计时发任何请求 —— 只有剩余%需要网络刷新。失败也推进
    _last_fetch_ts(失败占频率预算,防 1s tick 对故障端点加密重试)。

    刷新档位 refresh(v0.7 刷新档位显式化):"auto"(缺省,上述事件驱动+
    节流)或固定间隔秒数(int,手动档纯间隔轮询,忽略活动/静默)。设置窗
    改档时 UI 侧 _apply_config 裸写 self.refresh 热更 —— 属性赋值在 GIL
    下原子,run() 每 tick 现读,最坏 1s 后生效;不重启线程,重启反而丢
    _last_fetch_ts 频率记账、换档立即重查一次白耗请求。

    启动位置钉死(评审必改#1):类定义在本模块,但只由 zcode_meter_qt 的
    MeterWindow 实例化与启动;DataEngine.__init__/run() 及一切测试路径永不
    触碰 —— 否则源码目录放着带 key 的 zm_config.json 时,回归测试会发真实
    网络请求。解析失败首跑把响应体片段(不含 key)截断落 zm_debug.log 便于
    修 parse,后续失败静默 debug,不崩不阻塞调度。"""

    TICK = 1.0                         # 节流决策的检查粒度(检查≠请求)
    TIMEOUT = 10.0

    def __init__(self, api_key: str, refresh="auto"):
        super().__init__(daemon=True, name="zm-quota")
        self.api_key = api_key
        # 档位值域见 _norm_quota_refresh;非法值不在此兜底(配置层已钳),
        # run() 侧对非 num 非 auto 的值防御性按 auto 走,线程绝不因档位死
        self.refresh = refresh
        self.stop_flag = threading.Event()
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._logged_parse_fail = False
        self._last_fetch_ts: float | None = None      # 上次真实请求时刻(成败都记)
        self._last_activity_ts: float | None = None   # 最近『有消耗』信号时刻

    def notify_activity(self, ts: float | None = None):
        """『有消耗』信号落点:DataEngine._check_activity 在 completed 水位
        前进时回调(引擎线程),本线程 1s tick 读。写侧持既有 _lock,读侧
        同锁对齐;ts=None 取当前时刻,显式 ts 供单测注入。"""
        t = time.time() if ts is None else float(ts)
        with self._lock:
            self._last_activity_ts = t

    def latest(self) -> dict | None:
        """UI 线程取最近一次解析结果(拷贝,避免跨线程共享可变 dict);
        fetched_at(本次查询时刻,UI 侧『N分钟前』新鲜度)由
        _fetch_and_record 附加而非 parse 产出,UI 以 .get 读。"""
        with self._lock:
            return dict(self._latest) if self._latest else None

    def run(self):
        first = True
        while not self.stop_flag.is_set():
            with self._lock:
                la = self._last_activity_ts
            # 档位每 tick 现读(热更生效点):手动档以档位秒数为 gap;
            # 非 auto 且非数值的档位防御性回退 auto —— 本循环无 try,
            # quota 线程绝不因一个坏档位值(外部直改 self.refresh)炸死,
            # 回退 auto(静默期暂停)也比回退 0(每秒狂查)安全
            mode = self.refresh
            if mode != "auto" and not _is_num(mode):
                mode = "auto"
            gap = float(mode) if mode != "auto" else 0.0
            # first 仅首轮 True:启动即查,与旧 run() 先查后等的行为一致
            if quota_fetch_decision(time.time(), self._last_fetch_ts, la,
                                    force=first, mode=mode, gap=gap):
                self._fetch_and_record()
            first = False
            self.stop_flag.wait(self.TICK)

    def _fetch_and_record(self):
        """一次真实请求 + 频率记账:无论成败都推进 _last_fetch_ts —— 失败
        若不占预算,1s tick 会对故障端点每秒重试,比旧盲轮询更凶。成功时
        把 fetched_at(数据时刻,新鲜度展示依据)随结果一并入库。"""
        data = self._fetch_once()
        ts = time.time()
        self._last_fetch_ts = ts
        if data is not None:
            data["fetched_at"] = ts
            with self._lock:
                self._latest = data

    def _fetch_once(self) -> dict | None:
        try:
            req = urllib.request.Request(
                QUOTA_URL,
                # 实测该接口 Authorization 头放原始 key,不带 Bearer 前缀
                headers={"Authorization": self.api_key})
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                body = resp.read(65536).decode("utf-8", "replace")
        except Exception as exc:       # URLError/超时/HTTP 错误一律降级,不崩
            dbg(f"quota fetch failed: {type(exc).__name__}")
            return None
        try:
            data = parse_quota_payload(json.loads(body))
        except Exception:
            data = None
        if data is None:
            self._log_parse_fail(body)
        return data

    def _log_parse_fail(self, body: str):
        if self._logged_parse_fail:
            dbg("quota parse failed again (raw not repeated)")
            return
        self._logged_parse_fail = True
        # 响应体不含 API key(请求头才带),可安全落盘;只截片段防巨型刷屏
        try:
            with open(DBG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} quota parse failed, "
                        f"raw[:800]: {body[:800]}\n")
        except OSError:
            pass

    def stop(self):
        self.stop_flag.set()


# ------------------------------------------------------------ 多用量源抽象 ---
# UsageSource/ZCodeSource/ClaudeSource(含 _claude_ts_local)已原样迁至
# sources/ 包(base.py / zcode.py / claude.py),SQL 与解析规则一字未动,
# 经顶部 re-import 维持全部旧导入路径;DataEngine.sources 改由
# discover_sources() 自动发现并按 (Source.order, 模块名) 排序 —— 迁移是
# 纯移动,源行为与既有单测断言零变化(test_claude_source_synthetic /
# test_today_by_source 钉死)。新增源见 README『如何贡献一个源』。


# ---------------------------------------------------------------- 数据层 ---

@dataclass
class Snapshot:
    """数据线程推给 UI 的一份快照。"""
    state: str = "idle"                 # idle | generating
    model: str = ""
    title: str = ""                     # 会话标题(跟随切换)
    tps_est: float | None = None        # 生成中估算 tok/s(实时)
    tps_exact: float | None = None      # 最近完成的请求精确 tok/s(实时)
    tps_avg: float | None = None        # 主力模型的会话平均 tok/s
    speed_by_model: list = None         # [(provider, model, tps, out_tokens)] 按用量降序
    # ---- v0.8.0 新增:速度趋势(sparkline 数据源) ----
    recent_speeds: list = None          # 全局吞吐的逐秒采样(最近 ~12 点,时间
                                        # 正序)—— v0.8.0 视觉对版期速度语义改
                                        # 全局瞬时吞吐后,sparkline 与主数字同
                                        # 口径(引擎 _gtps_hist 环形缓冲填充);
                                        # <2 点时 UI 隐藏曲线(不闪空)
    global_tps: float = 0.0             # 全局瞬时吞吐 tok/s = 全部会话(主/子代
                                        # 理/工作流/compact 等)流式贡献 + 完成
                                        # 请求与最近 10s 窗口的重叠加权;0=空闲
    today_tokens: int = 0               # 今日全部会话 token 总用量(in+out)
    # ---- v0.4.0 新增(口径见 README):金额/燃速/多源均 ZCode-DB-only ----
    today_cost_cny: float = 0.0         # 今日金额(元,按刊例价;订阅套餐内
                                        # 实际不按量扣费,此为等值成本估算)
    today_cost_partial: bool = False    # 含未知模型 → 金额为下限(UI 加 ≈)
    burn_tokens_per_hour: float = 0.0   # 燃速 = trailing 60min 窗口 token 和
    burn_avg_tokens_per_hour: float | None = None   # 会话平均燃速 Σ(in+out)/Σ净生成h
    burn_instant_per_hour: float | None = None  # 瞬时燃速=最近请求(in+out)/净生成h
    burn_cny_per_hour: float = 0.0      # 燃速金额版(元/h,同窗口)
    est_hours_left: float | None = None # (日预算-今日花费)/燃速;未配预算或
                                        # 燃速 0 → None(活跃不足 60min 会低估)
    today_by_source: list = None        # [(源名, 今日token)] 聚合展示;今日用量
                                        # 本体 today_tokens 仍钉死 ZCode-DB-only
    plan_remaining_pct: float | None = None  # quota 轨 5h 窗剩余%,由 UI 线程
                                             # 从 QuotaMonitor 回填(引擎不触碰)
    last_ttft: float | None = None      # 最近完成请求的首字等待(s)
    last_duration: float | None = None  # 最近完成请求的整体耗时(s)
    session_in: int = 0                 # 总输入(input_tokens 已含缓存命中,勿再加 cache)
    session_cache: int = 0              # 其中缓存命中的部分
    session_out: int = 0
    cache_rate: float = 0.0             # cache / in
    gen_elapsed: float = 0.0
    manual: bool = False                # 统计对象被手动固定(卡片标题前缀 📌)
    updated: float = field(default_factory=time.time)


class DataEngine(threading.Thread):
    POLL_DB = 1.0
    POLL_PART = 0.5
    CHAR_PER_TOKEN_INIT = 3.2
    THROUGHPUT_WINDOW_S = 10.0   # 全局吞吐的完成重叠窗(用户拍板 10s;越小越
                                 # "瞬时"越抖,越大越平滑越迟钝)

    def __init__(self, out: "queue.Queue[Snapshot]"):
        super().__init__(daemon=True)
        self.out = out
        self.stop_flag = threading.Event()
        self.snap_lock = threading.RLock()         # 可重入:保护 snap 读写一致(多线程)
        self.manual_session: str | None = None     # 手动固定统计会话;None=自动跟随
        self.session_id = self._latest_session()
        self.snap = Snapshot()
        self._running = False
        self._gen_start = 0.0
        self._last_len: int | None = None
        self._last_len_t = 0.0
        self._chars_per_token = self.CHAR_PER_TOKEN_INIT
        self._exact_out_chars = 0
        # ---- 全局瞬时吞吐(v0.8.0 对版期,速度语义从『当前会话』改『机器
        # 全部会话』):_gpart_last = 每会话最新 text part 的 (长度, 时刻),
        # 增长即流式输出;_gtps_hist = 全局吞吐逐秒采样环形缓冲(sparkline) --
        self._gpart_last: dict = {}
        self._gtps_hist: "deque" = deque(maxlen=12)
        self._prev_max_rowid = self._max_usage_rowid()
        # ---- v0.5.1:quota 活动信号。水位 = completed 行最大 rowid(全
        # 会话全 query_source,含 subagent —— 与今日用量同宽,子代理消耗
        # 同样算『有消耗』,刻意设计);构造时对齐现值,存量行不触发。
        # on_activity 由 UI 接到 QuotaMonitor.notify_activity;未接线(一切
        # 测试路径)时 _check_activity 对 None 短路,行为与旧版逐位一致 ----
        self.on_activity = None
        self._act_rowid = self._max_completed_rowid()
        # ---- v0.4.0:金额/燃速/多源。全部本地只读,不触网络不落盘,
        # QuotaMonitor 仍只由 MeterWindow 实例化(启动位置钉死,评审#1) ----
        cfg = load_config()
        self.prices = load_prices()
        self.daily_budget_cny = cfg["daily_budget_cny"]
        # discover_sources() 自动发现 sources/ 包内源并按 order 排序(现 =
        # ZCode(0)、Claude(10)),与迁移前硬编码 [ZCodeSource(), ClaudeSource()]
        # 顺序一致 —— 卡片 today_by_source 显示顺序不变
        self.sources: list = discover_sources()
        self.quota_hint = None           # UI 线程回写的 nextResetTime(ms),纯数据

    def _latest_session(self) -> str:
        """活跃会话 = part 表最新写入行的 session(part 是流式实时写的,
        生成一开始就能识别;rollout mtime 要等请求完成,会滞后一整轮)。
        必须排除 subagent 会话:子代理的流式输出同样写 part 表,
        会把统计劫持到子代理头上。"""
        try:
            con = self._connect()
            (sid,) = con.execute(
                "SELECT session_id FROM part"
                " WHERE session_id NOT LIKE 'sess_subagent%'"
                "   AND session_id NOT LIKE 'sess_dwf-%'"
                " ORDER BY rowid DESC LIMIT 1").fetchone()
            con.close()
            return sid or ""
        except (sqlite3.Error, TypeError):
            # 兜底:part 不可用时退回 rollout mtime
            try:
                best, best_t = "", 0
                for name in os.listdir(ROLL_DIR):
                    if name.startswith("model-io-sess_") and name.endswith(".jsonl"):
                        p = os.path.join(ROLL_DIR, name)
                        t = os.path.getmtime(p)
                        if t > best_t:
                            best_t, best = t, name[len("model-io-"):-len(".jsonl")]
                return best
            except OSError:
                return ""

    def _connect(self) -> sqlite3.Connection:
        return connect_ro()

    def _max_usage_rowid(self) -> int:
        try:
            con = self._connect()
            (n,) = con.execute("SELECT COALESCE(MAX(rowid),0) FROM model_usage").fetchone()
            con.close()
            return n
        except sqlite3.Error:
            return 0

    def _max_completed_rowid(self) -> int:
        """活动水位:completed 行的最大 rowid(不筛 query_source —— 消耗
        就是消耗;cancelled 行 status 不同,天然不计)。单次查询在真实库
        实测 ~0.004ms,_db_loop 每秒一查可忽略。"""
        try:
            con = self._connect()
            (n,) = con.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM model_usage"
                " WHERE status='completed'").fetchone()
            con.close()
            return n
        except sqlite3.Error:
            return 0

    def _check_activity(self):
        """_db_loop 每 tick 调用:completed 水位前进 = ZCode 有新请求完成
        = 『有消耗』→ 回调 on_activity(quota 节流的触发源)。入口先取回调
        并对 None 短路(未接线时不查水位,测试路径零开销);先推水位再回调
        (同一批行不重复触发);坏回调由外层 Exception 兜住,绝不拖死
        _db_loop —— 回调是 UI 接线的外部代码,不能让它带着轮询线程陪葬。"""
        cb = self.on_activity
        if cb is None:
            return
        hi = self._max_completed_rowid()
        if hi <= self._act_rowid:
            return
        self._act_rowid = hi
        try:
            cb()
        except Exception:
            pass

    # ---- 日志 tail ----
    def _tail_loop(self):
        path = self._log_path()
        f = None
        pos = os.path.getsize(path) if os.path.exists(path) else 0
        while not self.stop_flag.is_set():
            try:
                newpath = self._log_path()
                if newpath != path:
                    path = newpath
                    if f: f.close()
                    f, pos = None, 0
                if not os.path.exists(path):
                    time.sleep(0.4); continue
                if f is None:
                    f = open(path, "r", encoding="utf-8", errors="replace")
                    f.seek(pos)
                line = f.readline()
                if not line:
                    time.sleep(0.25); continue
                pos = f.tell()
                self._handle_log_line(line)
            except OSError:
                time.sleep(1)

    def _log_path(self) -> str:
        day = dt.date.today().strftime("%Y-%m-%d")
        return os.path.join(LOG_DIR, f"zcode-{day}.jsonl")

    def _handle_log_line(self, line: str):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        ev = obj.get("event", "")
        if ev == "model.request.started":
            self._running = True
            self._gen_start = time.time()
            self._last_len = None
        elif ev in ("model.request.completed", "model.sdk.stream.completed"):
            if self._running:
                self._running = False
                self._on_request_done()

    # ---- db 轮询 ----
    def _db_loop(self):
        while not self.stop_flag.is_set():
            self._poll_stats()
            self._check_activity()          # v0.5.1:水位前进 → quota 活动信号
            if not self._running:
                self._poll_new_completed()
            self.stop_flag.wait(self.POLL_DB)

    def _poll_stats(self):
        try:
            with self.snap_lock:                    # 与会话切换互斥,防半更新快照
                con = self._connect()
                sums = con.execute(
                    "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_input_tokens),0),"
                    " COALESCE(SUM(output_tokens),0)"
                    " FROM model_usage WHERE status='completed' AND session_id=?"
                    " AND query_source='main_turn'",
                    (self.session_id,)).fetchone()
                # 按 提供商+模型 分组的均速(Σ输出token ÷ Σ净生成时长),按总用量降序
                speed_rows = con.execute(
                    "SELECT provider_id, model_id, SUM(output_tokens),"
                    " COALESCE(SUM(MAX(duration_ms - COALESCE(time_to_first_token_ms,0), 1)),0),"
                    " COALESCE(SUM(input_tokens),0)"
                    " FROM model_usage WHERE status='completed' AND session_id=?"
                    " AND query_source='main_turn' AND duration_ms IS NOT NULL"
                    " GROUP BY provider_id, model_id"
                    " ORDER BY SUM(input_tokens)+SUM(output_tokens) DESC",
                    (self.session_id,)).fetchall()
                speed_by_model = [
                    (p, m, (o / (d / 1000)) if o and d else None, o)
                    for p, m, o, d, _i in speed_rows]
                # 瞬时燃速 = 最近一次完成请求的吞吐(单请求 in+out/净生成),
                # 体现"此刻"消耗;预算告警仍用 60min 窗口值(告警不该被抖动触发)
                rlast = con.execute(
                    "SELECT COALESCE(input_tokens,0)+COALESCE(output_tokens,0),"
                    " COALESCE(duration_ms,0) - COALESCE(time_to_first_token_ms,0)"
                    " FROM model_usage WHERE status='completed' AND session_id=?"
                    " AND query_source='main_turn' AND duration_ms IS NOT NULL"
                    " ORDER BY rowid DESC LIMIT 1", (self.session_id,)).fetchone()
                if rlast and rlast[0] and rlast[1] > 0:
                    self.snap.burn_instant_per_hour = rlast[0] / (rlast[1] / 3_600_000)
                else:
                    self.snap.burn_instant_per_hour = None
                # 会话平均燃速 = Σ(in+out)/Σ净生成时长(实时燃速的 in+out
                # 口径 × tps_avg 的会话时长口径 —— 两个既有口径的自然组合)
                tot_tok = sum((r[4] or 0) + (r[2] or 0) for r in speed_rows)
                tot_gen_ms = sum(r[3] or 0 for r in speed_rows)
                self.snap.burn_avg_tokens_per_hour = (
                    tot_tok / (tot_gen_ms / 3_600_000)) if tot_gen_ms else None
                today0 = today0_ms()
                # 今日用量=全部真实消耗(main_turn+subagent 等所有来源),
                # 与 ZCode 自身统计口径一致;cancelled 请求 token 为 0 无影响
                (today,) = con.execute(
                    "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                    " FROM model_usage WHERE status='completed' AND started_at>=?",
                    (today0,)).fetchone()
                # ---- v0.4.0 追加 SQL(上方三条原样不动):金额按 提供商+模型
                # 分组计价(与 today_tokens 完全同 WHERE 同天界);燃速取
                # trailing 60 分钟窗口(同 WHERE)。金额口径钉死 ZCode-DB-only:
                # Claude 等其他源只进 today_by_source,绝不进金额与燃速
                # (DEFAULT_PRICES 无 Claude 模型,计入会永久点亮 ≈)。----
                cost_rows = con.execute(
                    "SELECT provider_id, model_id, SUM(input_tokens),"
                    " SUM(cache_read_input_tokens), SUM(output_tokens)"
                    " FROM model_usage WHERE status='completed' AND started_at>=?"
                    " GROUP BY provider_id, model_id",
                    (today0,)).fetchall()
                win0 = int(time.time() * 1000) - 3_600_000
                burn_rows = con.execute(
                    "SELECT provider_id, model_id, SUM(input_tokens),"
                    " SUM(cache_read_input_tokens), SUM(output_tokens)"
                    " FROM model_usage WHERE status='completed' AND started_at>=?"
                    " GROUP BY provider_id, model_id",
                    (win0,)).fetchall()
                con.close()
                cost_cny, cost_partial = 0.0, False
                for _prov, model, i_, c_, o_ in cost_rows:
                    v_, p_ = cost_of(self.prices, model, i_ or 0, o_ or 0, c_ or 0)
                    cost_cny += v_
                    cost_partial = cost_partial or p_
                burn_tok, burn_cny = 0, 0.0
                for _prov, model, i_, c_, o_ in burn_rows:
                    burn_tok += (i_ or 0) + (o_ or 0)
                    v_, _p = cost_of(self.prices, model, i_ or 0, o_ or 0, c_ or 0)
                    burn_cny += v_
                self.snap.today_cost_cny = round(cost_cny, 4)
                self.snap.today_cost_partial = cost_partial
                # trailing 60min 之和即每小时燃速(窗口宽恰为 1h)
                self.snap.burn_tokens_per_hour = float(burn_tok)
                self.snap.burn_cny_per_hour = round(burn_cny, 6)
                self.snap.est_hours_left = est_hours_left(
                    self.daily_budget_cny, cost_cny, burn_cny)
                # 速度趋势 sparkline:全局吞吐逐秒采样环形缓冲(v0.8.0 对版期
                # 速度语义改全局,与主数字同口径;12 点 × 1s 采样 ≈ 12s 走势)
                now_t = time.time()
                self.snap.global_tps = (self._global_part_tps(now_t)
                                        + self._global_completed_tps(
                                            now_t, self.THROUGHPUT_WINDOW_S))
                self._gtps_hist.append(self.snap.global_tps)
                self.snap.recent_speeds = list(self._gtps_hist)
                # 多源聚合(只读、TTL 缓存);新字段与本批同批填充,
                # _switch_session 重建 Snapshot 后经 _poll_stats 立即补全不闪空
                self.snap.today_by_source = [
                    (s.name, s.today_usage()) for s in self.sources
                    if s.is_available()]
                self.snap.session_in, self.snap.session_cache, self.snap.session_out = sums
                self.snap.speed_by_model = speed_by_model
                self.snap.tps_avg = speed_by_model[0][2] if speed_by_model else None
                self.snap.today_tokens = today
                # input_tokens 已含 cache_read:命中率 = cache / in,分母不再加 cache
                self.snap.cache_rate = (self.snap.session_cache / self.snap.session_in * 100
                                        if self.snap.session_in else 0.0)
                if speed_by_model:
                    self.snap.model = speed_by_model[0][1]
        except sqlite3.Error:
            pass

    def fetch_recent_speeds(self, n: int = 12, session_id: str | None = None) -> list:
        """速度趋势(sparkline 数据源):某会话最近 n 条完成请求的逐条速度
        (tok/s),时间正序 [speed, …]。口径与 _poll_stats 的主速度查询完全
        同型(status='completed' AND session_id AND query_source='main_turn',
        v0.2.0 subagent 劫持会话统计的事故红线 —— 混入即曲线与主速度口径
        不符),再叠加 duration_ms IS NOT NULL AND output_tokens>0:分母/分子
        无意义的行不进曲线;rowid 判据与 rlast 查询一致(ORDER BY rowid DESC
        取最近 n 条,Python 侧 reversed 反转成时间正序 —— 曲线横轴左旧右新)。
        speed = output / (max(duration−COALESCE(ttft,0), 1)/1000),max(,1)
        防零除与 _poll_new_completed 同式。异常语义:sqlite3.Error → 返回 []
        (与 _poll_stats 同吞法,查询坏库不炸引擎线程,UI 按 <2 点隐藏);
        con.close 置 finally —— 查询抛错也不泄漏连接(con.close 后再查询
        会被 except 吞掉,连接必须先查后关)。session_id 缺省取
        self.session_id;显式传参供单测与其他会话查询。
        """
        sid = self.session_id if session_id is None else session_id
        try:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT output_tokens, duration_ms, time_to_first_token_ms"
                    " FROM model_usage WHERE status='completed' AND session_id=?"
                    " AND query_source='main_turn' AND duration_ms IS NOT NULL"
                    " AND output_tokens>0"
                    " ORDER BY rowid DESC LIMIT ?", (sid, n)).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []
        out = []
        for out_tok, dur_ms, ttft_ms in reversed(rows):
            gen_ms = max((dur_ms or 0) - (ttft_ms or 0), 1)
            out.append(out_tok / (gen_ms / 1000))
        return out

    def _poll_new_completed(self):
        try:
            con = self._connect()
            rows = con.execute(
                "SELECT rowid, output_tokens, duration_ms, time_to_first_token_ms"
                " FROM model_usage WHERE status='completed' AND session_id=?"
                " AND query_source='main_turn' AND rowid>?"
                " ORDER BY rowid", (self.session_id, self._prev_max_rowid)).fetchall()
            con.close()
        except sqlite3.Error:
            return
        for rid, out_tok, dur_ms, ttft_ms in rows:
            self._prev_max_rowid = max(self._prev_max_rowid, rid)
            gen_ms = max((dur_ms or 0) - (ttft_ms or 0), 1)
            if out_tok:
                self.snap.tps_exact = out_tok / (gen_ms / 1000)
                self.snap.last_ttft = (ttft_ms or 0) / 1000
                self.snap.last_duration = (dur_ms or 0) / 1000

    # ---- 全局瞬时吞吐(2026-09-28,速度语义『当前会话』→『机器全部会话』) ----
    def _global_part_tps(self, now: float) -> float:
        """流式贡献:所有会话最新 text part 的长度增长之和 ÷ 字符token比。

        part 表每个会话都在流式写入(子代理/工作流同样),取每会话 MAX(rowid)
        的 text 行长度,与上次采样差分 —— 增长即输出速率;行切换(新一轮开始,
        新行更短)只重置基线不计负增长。扫近 4000 行窗口防全表 GROUP BY;会话
        消失(生成完)时裁剪字典防无限长。chars/token 用主会话完成请求自校准
        的 _chars_per_token(子代理内容比例近似,初始化 3.2)。"""
        try:
            con = self._connect()
            rows = con.execute(
                "SELECT session_id, MAX(rowid), length(data) FROM part"
                " WHERE data LIKE '{\"type\":\"text\"%'"
                " AND rowid > (SELECT MAX(rowid) FROM part) - 4000"
                " GROUP BY session_id").fetchall()
            con.close()
        except sqlite3.Error:
            return 0.0
        tps = 0.0
        seen = set()
        for sid, _mx, ln in rows:
            if not sid or not ln:
                continue
            seen.add(sid)
            prev = self._gpart_last.get(sid)
            if prev is not None and ln > prev[0]:
                dt = max(now - prev[1], 1e-3)
                tps += (ln - prev[0]) / dt / self._chars_per_token
            self._gpart_last[sid] = (ln, now)
        if len(self._gpart_last) > len(seen) + 64:
            self._gpart_last = {k: v for k, v in self._gpart_last.items()
                                if k in seen}
        return tps

    def _global_completed_tps(self, now: float, window_s: float) -> float:
        """完成贡献:completed 行的输出区间 [first_token_at, completed_at] 与
        最近 window_s 窗口的重叠加权 —— 贡献 = r_i × 重叠秒 / 窗口,其中
        r_i = output/(c−ft)。token 摊到真实生成的那段时间上,完成瞬间不产生
        尖峰;流式请求完成后由 part 增长无缝切换到本项(不重不漏)。
        completed_at≥窗沿即全部候选行。异常语义:sqlite3.Error → 0.0。"""
        cut_ms = int((now - window_s) * 1000)
        try:
            con = self._connect()
            rows = con.execute(
                "SELECT output_tokens, first_token_at, completed_at"
                " FROM model_usage WHERE status='completed' AND completed_at>=?"
                " AND output_tokens>0 AND first_token_at IS NOT NULL",
                (cut_ms,)).fetchall()
            con.close()
        except sqlite3.Error:
            return 0.0
        win = max(window_s, 1e-3)
        now_ms = now * 1000.0
        tps = 0.0
        for out_tok, ft, c in rows:
            gen_s = max((c - ft) / 1000.0, 1e-3)
            ov_ms = min(c, now_ms) - max(ft, cut_ms)
            if ov_ms > 0:
                tps += (out_tok / gen_s) * (ov_ms / 1000.0) / win
        return tps

    # ---- part 轮询(生成中估算) ----
    def _part_loop(self):
        while not self.stop_flag.is_set():
            if self._running:
                self._poll_part()
            else:
                self._last_len = None
            self.stop_flag.wait(self.POLL_PART)

    def _poll_part(self):
        try:
            con = self._connect()
            row = con.execute(
                "SELECT length(data) FROM part"
                " WHERE session_id=? AND data LIKE '{\"type\":\"text\"%'"
                " ORDER BY rowid DESC LIMIT 1", (self.session_id,)).fetchone()
            con.close()
        except sqlite3.Error:
            return
        if not row or row[0] is None:
            return
        ln, now = row[0], time.time()
        if self._last_len is not None and ln > self._last_len:
            cps = (ln - self._last_len) / max(now - self._last_len_t, 1e-3)
            self.snap.tps_est = cps / self._chars_per_token
        self._last_len, self._last_len_t = ln, now

    # ---- 请求完成 ----
    def _on_request_done(self):
        if self._last_len:
            self._exact_out_chars = self._last_len
        try:
            con = self._connect()
            row = con.execute(
                "SELECT output_tokens, duration_ms, time_to_first_token_ms FROM model_usage"
                " WHERE status='completed' AND session_id=?"
                " ORDER BY rowid DESC LIMIT 1", (self.session_id,)).fetchone()
            con.close()
            if row and row[0]:
                out_tok, dur_ms, ttft_ms = row
                gen_ms = max((dur_ms or 0) - (ttft_ms or 0), 1)
                self.snap.tps_exact = out_tok / (gen_ms / 1000)
                self.snap.last_ttft = (ttft_ms or 0) / 1000
                self.snap.last_duration = (dur_ms or 0) / 1000
                if self._exact_out_chars:
                    self._chars_per_token = max(self._exact_out_chars / out_tok, 0.5)
        except sqlite3.Error:
            pass
        self.snap.tps_est = None
        self._push()

    # ---- 会话跟随:part 最新写入的会话变了 = 活跃会话切换 ----
    def _refresh_session(self):
        # 整体持锁(含 manual 检查与 _latest_session 查询):否则 UI 线程
        # set_manual_session 在「检查 manual→查最新会话」之间挤入,固定会被
        # 一次已过检查的自动切换覆盖,且永不自愈。RLock 可重入,_switch_session
        # 内再取锁不死锁;_latest_session 热缓存 ~1ms,持锁代价可接受。
        with self.snap_lock:
            if self.manual_session:                 # 手动固定期间不跟随
                return
            new = self._latest_session()
            if not new or new == self.session_id:
                return
            self._switch_session(new)

    def _switch_session(self, new: str):
        """切到指定会话(自动跟随/手动固定共用)。序列不可乱:
        - 先重建 Snapshot:防数据线程读到半更新快照(v0.2.0 修过的老 bug)
        - _last_len=None:否则用两会话 part 长度差算出错误 tps_est
        - _prev_max_rowid 重置:否则旧会话基线带进新会话,漏读/重读完成请求"""
        with self.snap_lock:
            self.session_id = new
            self.snap = Snapshot(state=self.snap.state, model=self.snap.model,
                                 tps_exact=self.snap.tps_exact,
                                 manual=bool(self.manual_session))
            self._last_len = None
            self._prev_max_rowid = self._max_usage_rowid()
            self.snap.title = self._session_title()
            self._poll_stats()
            self._init_tps_for_session()
            self._push()

    # ---- 手动固定 / 恢复自动(UI 线程调用) ----
    def set_manual_session(self, sid: str):
        """固定统计对象为 sid。与当前相同的会话也走完整切换:snap.manual
        标记(📌)必须随重建生效。"""
        with self.snap_lock:
            if not sid:
                return
            self.manual_session = sid
            self._switch_session(sid)

    def clear_manual_session(self):
        """恢复自动跟随,并立即对齐当前最新活跃会话。"""
        with self.snap_lock:
            self.manual_session = None
            self._switch_session(self._latest_session() or self.session_id)

    # ---- UI 侧查询(右键会话菜单 / 历史图表窗口,均只读独立连接) ----
    def recent_sessions(self, limit: int = 8) -> list:
        """最近会话(手动切换菜单用):按 part 每会话最新写入倒序、排除
        subagent,与 _latest_session 跟随口径同源。LEFT JOIN 取标题:
        标题缺失时列出空标题而非丢会话(与 _session_title 行为一致)。"""
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT p.session_id, s.title FROM"
                " (SELECT session_id, MAX(rowid) AS mr FROM part"
                "  WHERE session_id NOT LIKE 'sess_subagent%'"
                "    AND session_id NOT LIKE 'sess_dwf-%' GROUP BY session_id) p"
                " LEFT JOIN session s ON s.id = p.session_id"
                " ORDER BY p.mr DESC LIMIT ?", (limit,)).fetchall()
            con.close()
            return [(sid, (title or "").strip()[:16]) for sid, title in rows]
        except sqlite3.Error:
            return []

    def fetch_daily_usage(self, days: int = 30) -> list:
        """按天 token 用量(二元组):completed 全来源 in+out,与今日用量
        同口径(input 已含 cache_read 勿重复加);起点=(days-1) 天前本地午夜,
        days=1 时与今日用量 SQL 完全同界。天界用 SQLite 'localtime',依赖
        OS 时区设置。v0.4.0 起历史图表改用金额版 fetch_daily_usage_cost(三元
        组),本方法保留二元组形状不动 —— HistoryWindow 的 dict() 转换与单测
        test_daily_usage_matches_today_scope 双双依赖此形状。
        v0.6.x 起受 MAX_SCAN_ROWS 行防御上限约束:仅统计最近 N 行,更早
        记录不计(防库增长后查询线性变慢,详见常量处注释)。"""
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT date(started_at/1000, 'unixepoch', 'localtime') AS d,"
                " COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)"
                " GROUP BY d ORDER BY d",
                (today0_ms() - (days - 1) * 86_400_000, MAX_SCAN_ROWS)).fetchall()
            con.close()
            return rows
        except sqlite3.Error:
            return []

    def fetch_total_usage(self) -> tuple:
        """全部历史总计 (tokens, cny, partial):completed 全来源 in+out(同今日
        口径)按模型分组计价后求和。刻意不加 MAX_SCAN_ROWS floor —— 总计的
        语义就是完整历史(防御上限是为图表性能,不为账目截断);当前库规模
        的分组 SUM 为毫秒级,一年后仍可接受。金额为刊例价下限(同今日口径)。"""
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT model_id, COALESCE(SUM(input_tokens),0),"
                " COALESCE(SUM(cache_read_input_tokens),0),"
                " COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed'"
                " GROUP BY model_id").fetchall()
            con.close()
        except sqlite3.Error:
            return (0, 0.0, False)
        tok_total, cny_total, partial = 0, 0.0, False
        for _m, i_, c_, o_ in rows:
            cny, p = cost_of(self.prices, _m, i_ or 0, o_ or 0, c_ or 0)
            tok_total += (i_ or 0) + (o_ or 0)
            cny_total += cny
            partial = partial or p
        return (tok_total, round(cny_total, 2), partial)

    def fetch_daily_usage_cost(self, days: int = 30) -> list:
        """按天 (date, tokens, cny):token 口径与 fetch_daily_usage 完全一致。
        该方法的二元组形状被 HistoryWindow.refresh 的 dict() 转换与单测
        test_daily_usage_matches_today_scope 双双依赖,金额版必须走本方法,
        改旧方法返回元数会直接崩图表。金额需按模型分组计价后在 Python 侧
        聚合(unknown 模型计 ¥0,partial 不在此体现 —— 天级 ¥ 图表恒为下限,
        卡片上的 ≈ 标记才是 partial 的展示位)。
        v0.6.x 起受 MAX_SCAN_ROWS 行防御上限约束(同 fetch_daily_usage):
        仅统计最近 N 行,更早记录不计。"""
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT date(started_at/1000, 'unixepoch', 'localtime') AS d,"
                " model_id, COALESCE(SUM(input_tokens),0),"
                " COALESCE(SUM(cache_read_input_tokens),0),"
                " COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)"
                " GROUP BY d, model_id ORDER BY d",
                (today0_ms() - (days - 1) * 86_400_000, MAX_SCAN_ROWS)).fetchall()
            con.close()
        except sqlite3.Error:
            return []
        agg = {}
        for d, model, i_, c_, o_ in rows:
            tok, cny = agg.get(d, (0, 0.0))
            v, _partial = cost_of(self.prices, model, i_ or 0, o_ or 0, c_ or 0)
            agg[d] = (tok + (i_ or 0) + (o_ or 0), cny + v)
        return [(d, t, round(c, 4)) for d, (t, c) in sorted(agg.items())]

    def fetch_daily_model_usage(self, days: int = 30) -> list:
        """按天×模型明细 [(date, model, in_tok, cache, out_tok, cny, partial)]:
        导出 CSV(右键菜单)与趋势外推的数据源。SQL 与 fetch_daily_usage_cost
        刻意保持同 WHERE 同 GROUP(completed 全部 query_source、本地午夜天界)
        —— 两处若漂移,导出的明细与按天图表互相矛盾,会被用户当 bug。
        cny/partial 逐行调 cost_of(唯一合法计价入口;input 已含 cache_read,
        手写公式曾在此双计翻车一个数量级),未知模型行 ¥0+partial 由 cost_of
        语义透传。同 fetch_daily_usage_cost 一样刻意镜像而不合并:其三元组
        形状被单测与 HistoryWindow 依赖,本方法七元组形状是导出/外推专属。同受 MAX_SCAN_ROWS 行防御上限约束
        (与 cost 版同 floor,库超上限后明细与图表一致截断)。"""
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT date(started_at/1000, 'unixepoch', 'localtime') AS d,"
                " model_id, COALESCE(SUM(input_tokens),0),"
                " COALESCE(SUM(cache_read_input_tokens),0),"
                " COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)"
                " GROUP BY d, model_id ORDER BY d, model_id",
                (today0_ms() - (days - 1) * 86_400_000, MAX_SCAN_ROWS)).fetchall()
            con.close()
        except sqlite3.Error:
            return []
        out = []
        for d, model, i_, c_, o_ in rows:
            cny, partial = cost_of(self.prices, model, i_ or 0, o_ or 0, c_ or 0)
            out.append((d, model, i_ or 0, c_ or 0, o_ or 0,
                        round(cny, 4), partial))
        return out

    BLOCK_MS = 5 * 3600 * 1000            # 5h 计费块宽(ms)

    def fetch_billing_blocks(self, blocks: int = 29) -> list:
        """5 小时计费块(历史图表第三页签):返回 [(block_start_ms, tokens,
        is_current)],当前块由 now 落桶判定。
        块界对齐:quota 轨可用时(nextResetTime 已由 UI 回写到 self.quota_hint)
        用 nextResetTime-k*5h 对齐平台真实计费窗;否则回退锚点=completed 的
        最早 started_at(query_source 全部 —— 措辞刻意区别于跨产品的"全来源"),
        此时块界为示意、非平台真实计费窗(UI 页签内已声明)。
        聚合口径同今日 token:completed 全部 query_source 的 in+out(input 已含
        cache_read 勿重复加)。v0.6.x 起锚点 MIN 子查询与聚合 SQL 均受
        MAX_SCAN_ROWS 行防御上限约束:仅统计最近 N 行,更早记录不计。"""
        try:
            con = connect_ro()
            anchor = self.quota_hint
            if not _is_num(anchor) or anchor <= 0:
                row = con.execute(
                    "SELECT MIN(started_at) FROM model_usage"
                    " WHERE status='completed'"
                    " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)",
                    (MAX_SCAN_ROWS,)).fetchone()
                anchor = row[0] if row and row[0] else None
            if not _is_num(anchor) or anchor <= 0:
                con.close()
                return []
            anchor = int(anchor)
            now_ms = int(time.time() * 1000)
            # floor 除法:quota 锚点在未来时 now-anchor 为负,向负取整恰好把
            # now 归入上一块(k=-1),与"当前块=[reset-5h, reset)"一致
            k_cur = (now_ms - anchor) // self.BLOCK_MS
            first = k_cur - blocks + 1
            base = anchor + first * self.BLOCK_MS
            if base < 0:
                con.close()
                return []
            rows = con.execute(
                "SELECT (started_at-?)/? AS b,"
                " COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " AND started_at<?"
                " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)"
                " GROUP BY b",
                (base, self.BLOCK_MS, base, base + blocks * self.BLOCK_MS,
                 MAX_SCAN_ROWS)).fetchall()
            con.close()
        except sqlite3.Error:
            return []
        buckets = {int(b): (t or 0) for b, t in rows}
        return [(base + i * self.BLOCK_MS, buckets.get(i, 0), first + i == k_cur)
                for i in range(blocks)]

    def fetch_session_usage(self, limit: int = 20) -> list:
        """按会话 token 用量(历史图表):completed + main_turn、排除 subagent
        会话 —— 与卡片统计逐字对齐(README 口径表)。返回 (sid,title,tokens,
        请求数),按会话最近请求时间倒序。注意:普通会话内也混有 compact/
        workflow_child 等非 main_turn 来源,不加 query_source 过滤必与卡片
        对不上而被当 bug 报。v0.6.x 起受 MAX_SCAN_ROWS 行防御上限约束:
        仅统计最近 N 行,更早记录不计(含整会话被裁出结果集的形态)。"""
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT mu.session_id, COALESCE(s.title,''),"
                " COALESCE(SUM(mu.input_tokens),0)+COALESCE(SUM(mu.output_tokens),0),"
                " COUNT(*)"
                " FROM model_usage mu LEFT JOIN session s ON s.id = mu.session_id"
                " WHERE mu.status='completed' AND mu.query_source='main_turn'"
                " AND mu.session_id NOT LIKE 'sess_subagent%'"
                " AND mu.session_id NOT LIKE 'sess_dwf-%'"
                " AND mu.rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)"
                " GROUP BY mu.session_id"
                " ORDER BY MAX(mu.started_at) DESC LIMIT ?",
                (MAX_SCAN_ROWS, limit)).fetchall()
            con.close()
            return rows
        except sqlite3.Error:
            return []

    def _session_title(self) -> str:
        try:
            con = self._connect()
            row = con.execute("SELECT title FROM session WHERE id=?",
                              (self.session_id,)).fetchone()
            con.close()
            return (row[0] or "").strip()[:16] if row else ""
        except sqlite3.Error:
            return ""

    def _init_tps_for_session(self):
        """切到新会话时,用该会话最近一次完成请求的速度作为初始精确值。"""
        try:
            con = self._connect()
            row = con.execute(
                "SELECT output_tokens, duration_ms, time_to_first_token_ms FROM model_usage"
                " WHERE status='completed' AND session_id=? AND query_source='main_turn'"
                " ORDER BY rowid DESC LIMIT 1", (self.session_id,)).fetchone()
            con.close()
            if row and row[0]:
                gen_ms = max((row[1] or 0) - (row[2] or 0), 1)
                self.snap.tps_exact = row[0] / (gen_ms / 1000)
                self.snap.last_ttft = (row[2] or 0) / 1000
                self.snap.last_duration = (row[1] or 0) / 1000
        except sqlite3.Error:
            pass

    # ---- 主循环 ----
    def run(self):
        if not self.session_id:
            return
        self.snap.title = self._session_title()
        self._poll_stats()
        self._init_tps_for_session()
        self._push()
        for target in (self._tail_loop, self._db_loop, self._part_loop):
            threading.Thread(target=target, daemon=True).start()
        last_state = None
        while not self.stop_flag.is_set():
            self._refresh_session()                  # 跟随 ZCode 会话切换
            state = "generating" if self._running else "idle"
            if state != last_state:
                last_state = state
                self.snap.state = state
                self._push()
            if self._running:
                self.snap.gen_elapsed = time.time() - self._gen_start
                self._push()
            time.sleep(0.5)

    def _push(self):
        self.snap.updated = time.time()
        try:
            self.out.put_nowait(self.snap)
        except queue.Full:
            pass

    def stop(self):
        self.stop_flag.set()


