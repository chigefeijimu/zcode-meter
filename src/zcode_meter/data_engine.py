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


# 皮肤白名单(9 款):glass=缺省(现玻璃仪表),其余 8 款视觉见
# design/skins-8x3.html 定稿。数据层只管配置键的合法性白名单;渲染与未实现
# id 的回退在 UI 层(skins.py registry 缺项回退玻璃)—— 白名单先行钉死
# 全部 9 个 id,手改文件指向尚未实现的皮肤也不在配置层报错
SKIN_IDS = ("glass", "swiss", "crt", "chalk", "liquid",
            "industrial", "newspaper", "vaporwave", "blueprint")


def _norm_skin(v):
    """skin(皮肤配置键)白名单归一化:仅 SKIN_IDS 内的 id 合法并原样返回,
    否则 None(镜像 _norm_quota_refresh 的纯函数口径)。元组成员判定对任意
    类型输入都安全(数字/bool/None/list 一律不匹配,不抛错),无需 isinstance
    预筛。缺省语义由调用方定:load_config 恒回 "glass"(键归一化后必在,
    消费方免 .get 兜底),save_config 非法/缺省整键省略(可选键纪律)。"""
    return v if v in SKIN_IDS else None


def load_config() -> dict:
    """zm_config.json(用户本地文件,已 .gitignore,防 key 随仓库提交):
    quota_api_key(str)、daily_budget_cny(>0 数字)、alert_pct(正数列表)、
    quota_refresh(可选:仅文件含合法值时含键,见 _norm_quota_refresh)、
    bar_segments(规范化后必含)、skin(归一化后必含,白名单外/缺省恒
    "glass",见 _norm_skin)。
    缺文件/坏 JSON/字段类型不对一律回退默认,不抛错。任何日志与调试路径
    都不得打印 key 明文(泄漏面专查项)。"""
    # skin 放默认 dict 而非仅函数末尾:缺文件/坏 JSON 的早退路径返回的也是
    # 这份 dict —— 恒含 "glass" 才算『消费方免 .get 兜底』,否则早退路径
    # 仍是无 skin 键形状,恒含就是半截承诺
    cfg = {"quota_api_key": "", "daily_budget_cny": None, "alert_pct": [20.0, 10.0],
           "skin": "glass"}
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
    # 皮肤键恒置(与 bar_segments 同款『归一化后必在』形状):白名单外/
    # 缺省一律回 "glass" —— 存量文件无此键零迁移,非法值(含手改错别字)
    # 不抛错静默回玻璃,save_config 侧对 glass 整键省略(见下方可选键纪律)
    cfg["skin"] = _norm_skin(obj.get("skin")) or "glass"
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
    # 皮肤可选键:仅白名单内且非缺省 glass 才写 —— 选玻璃=整键省略(镜像
    # quota_refresh="auto" 省键注释:缺省值落盘只会让旧文件平白多一个无
    # 信息量的键、翻掉三键形状断言);白名单外非法值同样省键,load_config
    # 读回恒得 glass(坏值不驻留文件)
    sk = _norm_skin(cfg.get("skin"))
    if sk is not None and sk != "glass":
        out["skin"] = sk
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
    # T2:completed 行取数(_refresh_completed_rows)的 started_at 前置回看窗。
    # 取数必须走 `started_at>=cut-本值 AND completed_at>=cut` 前置形态才能命中
    # model_usage_started_model_idx(started_at 前缀)—— 纯 completed_at>=? 谓词
    # 无索引可用,每秒全表 SCAN model_usage(两轮实测 39.7/33.71ms;把旧 SQL
    # 字面照搬当缓存刷新用同样 SCAN,实测 41.37ms/次,评审已否决)。2h 的依据:
    # 本库 MAX(completed_at-started_at)=MAX(duration_ms)=3,852,050ms≈64.2min、
    # 超过 2h 的行数=0 —— 因此 started_at<cut-2h 且 completed_at>=cut(即单次
    # 请求持续>2h)的行会被本形态排除,属设计边界(B3'):旧 completed_at-only
    # 形态会计入这类行,本库实测零行、正常请求不存在,常量在此即声明。
    COMPLETED_LOOKBACK_MS = 2 * 3_600_000
    # T6:Claude watcher 合并防抖窗。watchdog 对一次 jsonl append 常连发多条
    # modified 事件,首事件后再等 0.1s 把同批突发并成一轮 note_changes ——
    # 增量解析次数 = 批次数而非事件数,而 0.1s 对『提前出数』的延迟贡献
    # 可忽略(事件路径本就 1s TTL 采样)。
    WATCH_DEBOUNCE_S = 0.1

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
        # 增长即流式输出;_gtps_hist = 全局吞吐逐秒采样环形缓冲(sparkline)。
        # v0.9 T1 水位走读根治(改前 4000 行窗口 SQL 被 GROUP BY session_id
        # 击穿成 part 全索引扫描,两轮实测 80.7/45.95ms,每秒一跑且全程持
        # snap_lock)新增三态,全部由 _global_part_tps 在引擎线程独占维护:
        # _part_watermark = part 表已走读到的最大 rowid(None=尚未建基线,首拍
        # 经 _rebuild_part_baselines 全量重建;刻意不在 __init__ 做 —— 重建含
        # ~42-56ms 全量 GROUP BY,属引擎线程首拍职责而非 UI 线程构造);
        # _sid_text_rowid = 每会话最新 text part 行 rowid(仅喂 IN 长度探针,
        # 与 _gpart_last 在同一裁剪点同步按 seen+64 裁剪,B5');_session_last_
        # rowid = 每会话最大 rowid(任何类型行,append 语义 = 旧 recent_
        # sessions 的 MAX(rowid) GROUP BY 增量化,走读喂点 + 回退全量重建;
        # 刻意全量不裁剪 —— T3 recent_sessions 菜单正确性依赖全历史会话,
        # 量级 = 会话数千级 × 每条几十字节 ≈ 几十 KB,远小于一行 part 数据)----
        self._gpart_last: dict = {}
        self._gtps_hist: "deque" = deque(maxlen=12)
        self._part_watermark: int | None = None
        self._sid_text_rowid: dict = {}
        self._session_last_rowid: dict = {}
        # T3:_session_last_rowid 的专属锁。读写双方:引擎写侧(_global_part_
        # tps 走读批量 update / _rebuild_part_baselines 整体替换,均已在
        # snap_lock 内)+ 菜单读侧(recent_sessions 快照拷贝 / 冷缓存回填
        # _rebuild_session_last_rowid,UI 线程)。锁序纪律(N3):唯一合法
        # 顺序 snap_lock→_session_lock —— 引擎写侧持 snap_lock 时再取本锁
        # 合法;菜单读侧只取本锁;严禁持本锁再取 snap_lock(反向死锁);严禁
        # 用 snap_lock 兜底缓存(菜单是低频 UI 路径,不得与引擎每秒 tick 争
        # 引擎主锁)。
        self._session_lock = threading.Lock()
        # ---- T2:completed 行缓存(_global_completed_tps 取数/加权拆分)。
        # 行集 [(output_tokens, first_token_at, completed_at)] 由
        # _refresh_completed_rows 用 started_at 前置形态整批换行;加权每拍用
        # 缓存+当前 now 重算 —— T4 闸门关闭期不刷新,重叠加权随 now 前移
        # 自然衰减(冻结 db 下与旧『每秒重查』逐拍全等)。_completed_rows_
        # cut_ms=None 表示从未取过数(_global_completed_tps 直调时兜底取一
        # 次)。两者只在 snap_lock 内读写(_poll_stats 调用路径天然持锁)。----
        self._completed_rows: list = []
        self._completed_rows_cut_ms: int | None = None
        # ---- T4:ZCode db 文件闸门 + today0 时间闸 + _wake 事件唤醒 ----
        # _wake = Claude watcher(T6)的提前出数信号:置位让 _db_loop 的
        # _wake.wait(POLL_DB) 立即返回执行一轮(SQL 段仍由闸门+时间闸独立
        # 判定,唤醒轮也可能撞上冻结的 db);stop() 双 set,停机不等满周期。
        self._wake = threading.Event()
        # 闸门指纹:db.sqlite 与 db.sqlite-wal 各自的 (st_mtime_ns, st_size)。
        # _gate_ready=False = 尚无基线(首拍必开闸,与旧『每 tick 全查』起步
        # 一致);None 是合法指纹 = 文件缺席(DELETE journal 模式/首启),必须
        # 与『从未见过』区分开 —— 否则 db 缺席的机器每拍都判开闸,空闲 0-SQL
        # 永不成立。_gate_today0 = 上次开闸拍的 today0_ms()(B1' 时间闸:跨
        # 午夜强制开闸,保午夜清零口径)。全部只由 _db_loop 线程读写。
        self._gate_ready = False
        self._gate_db_sig: "tuple | None" = None
        self._gate_wal_sig: "tuple | None" = None
        self._gate_today0: int | None = None
        # 引擎侧 per-source today 值缓存(只装闸门管辖的 ZCode 源分量):
        # 闸门开拍刷新 —— ZCodeSource.today_usage 的 SQL 属闸门管辖段
        # (db 未变 ⇔ 今日分量必不变);关门拍 today_by_source 列表照常
        # 每拍重建,但 ZCode 分量取缓存、非 DB 分量(Claude)现调 —— 故
        # 刻意不把非 DB 分量装进缓存(装了也永不被读,白付一次调用)。
        self._src_today_cache: dict = {}
        # 变化即 push 的『易变字段』指纹(上拍值);None=尚无基线。
        self._push_fp: "tuple | None" = None
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
        # ---- T6:Claude watcher 的待处理事件队列(.jsonl 路径)。实例属性而
        # 非 _claude_watch_loop 内局部变量:注入式单测不经 watchdog、不发真实
        # FS 事件,直接向它喂合成事件即可驱动完整分发路径(验收钉死『测试机
        # 无 watchdog 也必须绿』)。单生产者(watchdog handler 经
        # _claude_watch_offer 投放)单消费者(watcher 线程 _claude_watch_pump),
        # queue.Queue 自身线程安全,无需另加锁 ----
        self._claude_watch_q: "queue.Queue" = queue.Queue()

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
            # 兜底:part 不可用时退回 rollout mtime。兜底必须沿用主路径的
            # 排除红线(sess_subagent*/sess_dwf-*):子代理/工作流的流式输出
            # 同样落 rollout 文件,且工作流运行期其 mtime 恰恰最新 —— 不排除
            # 则兜底把统计劫持到子代理头上,等于 v0.2.0 主路径事故在兜底路径
            # 复发(实测 rollout 目录曾一度仅剩 dwf 文件,劫持 100% 命中)。
            try:
                best, best_t = "", 0
                for name in os.listdir(ROLL_DIR):
                    if not (name.startswith("model-io-sess_")
                            and name.endswith(".jsonl")):
                        continue
                    sid = name[len("model-io-"):-len(".jsonl")]
                    if sid.startswith(("sess_subagent", "sess_dwf-")):
                        continue
                    p = os.path.join(ROLL_DIR, name)
                    t = os.path.getmtime(p)
                    if t > best_t:
                        best_t, best = t, sid
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
        if ev not in ("model.request.started", "model.request.completed",
                      "model.sdk.stream.completed"):
            return
        # 会话排除红线(与 _latest_session/fetch_session_usage 同款):dwf/
        # subagent 会话(sess_dwf-*/sess_subagent*)的请求事件绝不能驱动
        # generating 状态机 —— 实测日志里 90%+ 的请求事件来自 dwf,一旦
        # 放行,空闲的主会话卡片会跟着子代理显示『生成中 Ns』+脉冲,
        # _poll_new_completed 被 _running 门控停摆、tps_est 冻结不清理
        # (『subagent 污染会话判定』教训在日志事件路径复发,2026-09-30 P1)。
        # 无 sessionId 键的行按旧路径处理(不因缺键丢事件)。
        sid = obj.get("sessionId")
        if isinstance(sid, str) and sid.startswith(("sess_subagent", "sess_dwf-")):
            return
        if ev == "model.request.started":
            self._running = True
            self._gen_start = time.time()
            self._last_len = None
        elif self._running:
            self._running = False
            self._on_request_done()

    # ---- db 轮询(T4:ZCode db 文件闸门 + today0 时间闸 + _wake 事件唤醒) ----
    def _db_loop(self):
        # _wake.wait 兼职节拍与唤醒:Claude watcher(T6)置位 → 立即执行一轮
        # (提前出数);超时 POLL_DB → 常规 1s 节拍。Event 语义保证不丢拍:
        # 置位发生在 wait 之外时,下一次 wait 立即返回。先 clear 再执行 ——
        # 执行期间再置位的事件留给下一拍,不丢不并。
        while not self.stop_flag.is_set():
            woken = self._wake.wait(self.POLL_DB)
            if woken:
                self._wake.clear()
            self._db_tick(woken)

    @staticmethod
    def _stat_sig(path: str, prev: "tuple | None") -> "tuple | None":
        """单文件闸门指纹 (st_mtime_ns, st_size)。stat OSError(文件不存在:
        DELETE journal 模式/首启)→ 返回 prev,该文件视为维持原状(N2)——
        绝不能把缺席当『变化』,否则 db 缺席/WAL 回收后的每一拍都假开闸,
        空闲 0-SQL 名存实亡。"""
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return prev

    def _db_tick(self, woken: bool):
        """_db_loop 单轮:闸门判定 → SQL 段/尾段 → 变化即 push。

        闸门管辖段(B4' 清单,db 未变 ⇒ 全部可跳过且无损)= 本 tick 内经
        _connect 发出的全部 ZCode DB SQL:_poll_stats 闸门段(会话 sums/
        speed 分组/rlast/today/cost/burn + _refresh_completed_rows 换行 +
        _global_part_tps 水位走读/定点探针)+ ZCodeSource.today_usage 分量
        (经 _src_today_cache 刷新)+ _check_activity(_max_completed_rowid
        水位:db 未变 ⇔ 水位必不变,quota 活动信号无损)+ _poll_new_
        completed(db 未变 ⇔ _prev_max_rowid 基线必不变)。
        尾段(_poll_stats 闸门段之后)每 tick 照跑:today_by_source 非 DB
        分量、T2 缓存衰减重算、_gtps_hist 采样、push 判定。

        note1:闸门重开首拍 part 差分的 Δt 含空闲期(_gpart_last 的时刻戳
        停在上次开闸拍),过渡拍 tps 被稀释低估一拍、下拍恢复 —— 与现 1s
        采样同粒度级的差异,不特殊处理。
        note4:_switch_session/_refresh_session/_on_request_done/图表 fetch_*
        等事件路径全量执行不受闸门管辖(它们直调 _poll_stats(),缺省开闸;
        _refresh_session→_latest_session 每 0.5s 一条既存查询同样豁免)。"""
        db_sig = self._stat_sig(DB_PATH, self._gate_db_sig)
        wal_sig = self._stat_sig(DB_PATH + "-wal", self._gate_wal_sig)
        today0 = today0_ms()
        # 两文件均未变才关门;-shm 不进闸门(读者也会更新它,纯假阳性)。
        # B1' 时间闸:today0_ms() 较上次开闸拍变化(跨午夜)→ 强制视为开闸
        # 一次 —— today/today_cost/ZCode 源分量这类『查询无时间参数、值却
        # 随本地天界变』的必须重算,午夜清零口径才保得住;顺带修复现状
        # 『空闲期跨午夜 UI 今日数不刷新』的潜伏缺口(重算后易变字段有变
        # 即触发下方 push)。
        gate_open = (not self._gate_ready
                     or db_sig != self._gate_db_sig
                     or wal_sig != self._gate_wal_sig
                     or today0 != self._gate_today0)
        self._poll_stats(gate_open=gate_open)
        if gate_open:
            self._check_activity()      # v0.5.1:水位前进 → quota 活动信号
            if not self._running:
                self._poll_new_completed()
            # 基线在 SQL 段执行后落地:开闸拍即使 SQL 因坏库被吞(值维持
            # 旧值),指纹也已消费 —— 空闲期不反复重试坏库(与旧『每秒
            # 重查坏库』相比少噪音,数据不比旧差:下次文件变化照常开闸)。
            self._gate_ready = True
            self._gate_db_sig, self._gate_wal_sig = db_sig, wal_sig
            self._gate_today0 = today0
        # 变化即 push:事件唤醒轮(watcher 刚报 Claude jsonl 有变,数字应
        # 尽快落地)直接 push;开闸/时间闸轮易变字段较上拍有变才 push,
        # 值未变不 push;关门轮永不 push —— 空闲期 UI 显示旧值本就是现状
        # 语义(空闲期无 push),与 B1' 燃速冻结容忍同一家族。
        fp = self._volatile_fp()
        if woken or (gate_open and fp != self._push_fp):
            self._push()
        self._push_fp = fp

    def _poll_stats(self, gate_open: bool = True):
        """今日统计一轮(T4 拆两段,闸门只管 ZCode DB SQL 段)。

        gate_open=True(缺省)= 全量执行闸门管辖段 SQL 并刷新 per-source
        today 值缓存 —— _db_loop 首拍与一切事件路径直调均走全量(note4:
        _switch_session/_refresh_session/_on_request_done/图表 fetch_* 等
        事件路径全量执行不受闸门管辖);False(文件闸门关且时间闸未触发)=
        跳过 ZCode SQL 段,snap 的 ZCode 派生字段保持上次开闸值(燃速滑窗
        冻结容忍+活动自愈,B1')。尾段每 tick 照跑(_poll_stats_tail)。
        异常语义与拆分前一致:sqlite3.Error 一律吞掉不穿透。"""
        part_tps = 0.0
        if gate_open:
            part_tps = self._poll_stats_gated()
        self._poll_stats_tail(part_tps)

    def _poll_stats_gated(self) -> float:
        """闸门管辖段:本 tick 内全部 ZCode DB SQL(拆分前的整段原样移入,
        SQL 一字未动),持 snap_lock 与会话切换互斥。返回 part 流式贡献供
        尾段求和;sqlite3.Error → 吞掉返回 0.0(拆分前同吞法,part 贡献
        该拍作 0.0)。"""
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
                # 速度趋势 sparkline 的 part 分量(全局吞吐逐秒采样环形缓冲,
                # v0.8.0 对版期速度语义改全局,与主数字同口径;12 点 × 1s 采样
                # ≈ 12s 走势);完成分量与采样推进在尾段(每 tick 照跑)。
                # T2:completed 行缓存换行(取数与加权拆分的取数侧,SQL=主查询
                # 同一 started_at 前置换行,严禁 completed_at-only 字面形态)
                # —— 本调用按 T4 设计落在闸门管辖段:仅闸门开/时间闸开才刷,
                # 关闭期 _global_completed_tps 用缓存+当前 now 衰减重算;期间
                # db 里被删的行最多残留贡献 THROUGHPUT_WINDOW_S(10s)即自然
                # 出窗,容忍。
                now_t = time.time()
                self._refresh_completed_rows(
                    int((now_t - self.THROUGHPUT_WINDOW_S) * 1000))
                part_tps = self._global_part_tps(now_t)
                # T4:per-source today 值缓存开闸拍刷新,且只装闸门管辖的
                # ZCode 源分量 —— ZCodeSource.today_usage 的 SQL 属闸门管辖段
                # (db 未变 ⇔ 今日分量必不变),关门拍由尾段取缓存构建列表;
                # 非 DB 分量(Claude)尾段每拍现调,不进缓存(装了也永不被
                # 读,白付一次调用)。_switch_session 重建 Snapshot 后经
                # _poll_stats(全量)立即补全不闪空。
                self._src_today_cache = {
                    s.name: s.today_usage() for s in self.sources
                    if isinstance(s, ZCodeSource) and s.is_available()}
                self.snap.session_in, self.snap.session_cache, self.snap.session_out = sums
                self.snap.speed_by_model = speed_by_model
                self.snap.tps_avg = speed_by_model[0][2] if speed_by_model else None
                self.snap.today_tokens = today
                # input_tokens 已含 cache_read:命中率 = cache / in,分母不再加 cache
                self.snap.cache_rate = (self.snap.session_cache / self.snap.session_in * 100
                                        if self.snap.session_in else 0.0)
                if speed_by_model:
                    self.snap.model = speed_by_model[0][1]
                return part_tps
        except sqlite3.Error:
            return 0.0

    def _poll_stats_tail(self, part_tps: float) -> None:
        """T4 尾段:闸门无关,每 tick 照跑(开闸拍在 SQL 段之后、关门拍独自
        执行)。刻意不持 snap_lock —— 空闲期 0 持锁(N6:尾段是空闲期唯一
        还在跑的引擎工作),字段写遵循 _poll_new_completed/run() 主循环的
        既有无锁纪律;本方法触达的全部状态(_completed_rows/_gtps_hist/
        _src_today_cache)单写者 = 引擎 _db_loop 线程(直调场景为调用线程
        串行),无跨线程竞争。

        B1'(燃速滑窗冻结容忍,裁决=容忍+自愈):关门期 burn_*(60min 滑窗)
        随 SQL 段冻结、活动恢复(闸门开)即自愈 —— 与现状 UI 表现一致(空闲
        期无 push,UI 本就显示旧值;T7 CHANGELOG 声明该语义)。global_tps
        的完成分量靠 T2 缓存 + 当前 now 重算,衰减照推不冻结;part 分量在
        关门拍为 0(冻结的 db 无流式增长,查询亦得 0,等价;重开首拍的
        Δt 稀释见 _db_tick note1)。"""
        try:
            now_t = time.time()
            self.snap.global_tps = (part_tps + self._global_completed_tps(
                now_t, self.THROUGHPUT_WINDOW_S))
            self._gtps_hist.append(self.snap.global_tps)
            self.snap.recent_speeds = list(self._gtps_hist)
            # today_by_source 列表每 tick 重建:ZCode 分量取开闸拍缓存(关门拍
            # 绝不发 ZCode SQL —— 空闲 0-SQL 的组成之一),非 DB 分量(Claude
            # 等)现调(源内部 TTL 缓存,现调代价 O(新增字节))。缓存缺项只
            # 可能出现在『从未开闸』的异常调用序,兜底现调保口径不缺行(正常
            # _db_loop 路径首拍必开闸,缺项不可达)。
            self.snap.today_by_source = [
                (s.name, self._src_today_cache[s.name]
                 if isinstance(s, ZCodeSource) and s.name in self._src_today_cache
                 else s.today_usage())
                for s in self.sources if s.is_available()]
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
        """流式贡献:所有会话最新 text part 行的长度增长之和 ÷ 字符token比。

        part 表每个会话都在流式写入(子代理/工作流同样),取每会话最新 text
        part 行长度,与上次采样差分 —— 增长即输出速率;行切换(新一轮开始,
        新行更短)只重置基线不计负增长;不筛会话(全局吞吐含子代理/工作流)。
        chars/token 用主会话完成请求自校准的 _chars_per_token(子代理内容比例
        近似,初始化 3.2)。

        v0.9 T1 根治(改前 80.7/45.95ms:4000 行窗口被 GROUP BY session_id
        击穿成 part 全索引扫描,每秒一跑且全程持 snap_lock),常态三步:
        ①水位 `SELECT COALESCE(MAX(rowid),0) FROM part`(实测 0.003ms)——
        MAX(rowid)<存量水位 = 删顶行/VACUUM 回退,转 _rebuild_part_baselines
        全量重建;②走读 `rowid>? ORDER BY rowid` 只读新行(20 行 0.033ms/
        100 行 0.217ms,成本随新行数线性)喂 _session_last_rowid(任何类型行)
        与 _sid_text_rowid(text 行);③`rowid IN (…)` 定点探针重读每会话最新
        text 行长度(29 键实测 0.043ms,INTEGER PRIMARY KEY 点查)—— 真实库
        213,694/434,238 part 行 time_updated>time_created,流式输出在既有
        rowid 内追加文本,纯『只看新行』会让流式贡献静默归零(口径级回归),
        in-place 增长只有定点重读可见;顺带让窗口外行的 in-place 增长恢复
        可见(旧 4000 行窗口外的静默增长,对齐 README 全局吞吐字面语义)。
        B3:水位/走读/探针三条 SQL 各自函数内吞 sqlite3.Error(走读/探针失败
        =该拍贡献 0.0、水位失败=维持旧水位下拍重试),绝不穿透 _poll_stats
        —— 回归 fixture 只建 part(session_id) 无 data 列(test_data_engine
        的 sparkline 接线断言要求 _gtps_hist 照常 append 0.0)。三查共用
        一次 connect_ro(单连接纪律同 _poll_stats:WAL 库连接开关 ~1ms 比三
        查合计 <0.1ms 贵一个数量级),con.close 置 finally 不泄漏。"""
        # 单连接纪律:水位/走读/探针三查共用一次 connect_ro(与 _poll_stats
        # 同款;WAL 库上一次连接开关实测 ~1ms,比三条查询本身合计 <0.1ms 贵
        # 一个数量级,逐查开连接会把亚毫秒路径放大回毫秒级)。连接失败=水位
        # 失败语义:维持旧水位,本拍贡献 0.0
        try:
            con = self._connect()
        except sqlite3.Error:
            return 0.0
        try:
            # ① 水位探测(O(1) 尾读)。失败=维持旧水位,本拍贡献 0.0
            try:
                (mx,) = con.execute(
                    "SELECT COALESCE(MAX(rowid),0) FROM part").fetchone()
            except sqlite3.Error:
                return 0.0
            wm = self._part_watermark
            if wm is None or mx < wm:
                # 首拍建基线 / MAX(rowid) 回退(删顶行/VACUUM):双基线全量重建
                # (B2:_gpart 窗口基线 + _session_last_rowid 全量 GROUP BY,T3 缓存
                # 依赖后者)。重建成功才推进水位 —— 半截重建会让 _session_last_
                # rowid 永久缺会话且再无补救窗口(走读只覆盖新行),失败保持旧
                # 水位下拍整体重试;重建拍基线刚重置,贡献恒 0.0
                if self._rebuild_part_baselines(now, con):
                    self._part_watermark = mx
                return 0.0
            walk_sids = set()
            if mx > wm:
                # ② 发现走读:只读水位之后的新行。任何类型行喂 _session_last_
                # rowid(rowid 递增 + ORDER BY rowid,后写覆盖 = 旧 MAX(rowid)
                # GROUP BY 的 append 语义);text 行另喂 _sid_text_rowid。走读
                # 失败=贡献 0.0 且水位不推进(下拍重读,绝不跳行)
                try:
                    rows = con.execute(
                        "SELECT rowid, session_id, length(data), substr(data,1,16)"
                        " FROM part WHERE rowid>? ORDER BY rowid",
                        (wm,)).fetchall()
                except sqlite3.Error:
                    return 0.0
                new_last = {}   # T3:本拍新行先积累,循环后一次 _session_lock 换入
                for rid, sid, _ln, prefix in rows:
                    if not sid:
                        continue
                    new_last[sid] = rid
                    # substr 取 16 字符(≥前缀 14 字符,留余量),Python 侧
                    # startswith 与旧 SQL LIKE '{"type":"text"%' 前缀匹配等价
                    # (闭引号把 "textbox" 之类同前缀类型名挡在外面);isinstance
                    # 守卫坏库里 data 非 TEXT 的行
                    if isinstance(prefix, str) and prefix.startswith('{"type":"text"'):
                        self._sid_text_rowid[sid] = rid
                    walk_sids.add(sid)
                if new_last:
                    # T3 锁序(N3):引擎写侧持 snap_lock(_poll_stats 调用链)时
                    # 取 _session_lock 是唯一合法顺序;菜单读侧(recent_sessions)
                    # 只取 _session_lock。批量一次换入 —— 菜单读到的要么是本拍
                    # 前要么是本拍后的完整 dict,永不读到半拍中间态
                    with self._session_lock:
                        self._session_last_rowid.update(new_last)
                # 水位推进到本拍实际读到的最大 rowid:水位探测与走读之间新到的
                # 行下一拍重读(dict 更新幂等,无损失);探测后顶行被删则下拍
                # MAX(rowid) 回落触发回退重建,水位不虚降
                self._part_watermark = max(mx, rows[-1][0]) if rows else mx
            # ③ 长度探针:对 _sid_text_rowid 当前键集做 rowid 点查 —— in-place
            # 增长(流式输出在既有 rowid 内追加文本)只有定点重读可见。分块防
            # SQLite 变量数上限(旧版默认 999;seen+64 裁剪后常态几十键,分块是
            # 突发并发的防御 —— 单条 IN 打爆变量上限会被 except 吞成整拍 0.0)
            if not self._sid_text_rowid:
                return 0.0
            try:
                lens = {}
                rids = list(self._sid_text_rowid.values())
                for i in range(0, len(rids), 400):
                    chunk = rids[i:i + 400]
                    q = ("SELECT rowid, length(data) FROM part WHERE rowid IN (%s)"
                         % ",".join("?" * len(chunk)))
                    for rid, ln in con.execute(q, chunk).fetchall():
                        lens[rid] = ln
            except sqlite3.Error:
                return 0.0        # 探针失败:基线维持,该拍贡献 0.0(不裁剪)
            # ④ 差分:与旧实现逐字同式 —— 增长才计贡献,行切换只重置基线不计
            # 负;基线对全部探针命中会话刷新时刻(静默会话也前移 t,长暂停后
            # 恢复增长不因 dt 跨多拍被稀释,与旧窗口 SQL 每拍全量重读同语义)
            tps = 0.0
            seen = set()
            dropped = []
            for sid, rid in self._sid_text_rowid.items():
                ln = lens.get(rid)
                if ln is None:
                    # IN 探针 miss:该 text 行已被删除(但表顶 rowid 未回落,不
                    # 走回退重建)→ 会话裁剪 —— 基线与探针键同步去引用,防死
                    # 基线对将来的新行算出假增长;会话再写新 text 行时经走读
                    # 重新入表、基线重置
                    dropped.append(sid)
                    continue
                if not sid or not ln:
                    continue
                prev = self._gpart_last.get(sid)
                # B5' 的 seen = 本拍有活动的会话(走读到新行,或长度较基线有变
                # 化);静默会话无贡献,超 seen+64 余量即被裁。活动会话被误裁的
                # 唯一窗口是『本拍既无新行又无长度变化』且余量已耗尽 —— 与旧
                # 4000 行窗口滚出同粒度级的边界,恢复写新行即重新入表
                if sid in walk_sids or prev is None or ln != prev[0]:
                    seen.add(sid)
                if prev is not None and ln > prev[0]:
                    dt = max(now - prev[1], 1e-3)
                    tps += (ln - prev[0]) / dt / self._chars_per_token
                self._gpart_last[sid] = (ln, now)
            for sid in dropped:
                self._sid_text_rowid.pop(sid, None)
                self._gpart_last.pop(sid, None)
            # B5' 裁剪策略钉死:两 dict 在同一裁剪点、同一 seen+64 谓词同步裁剪
            # (_sid_text_rowid 仅喂探针,会话静默即无贡献,防 IN 变量数随历史会
            # 话无界涨);_session_last_rowid 刻意不裁剪 —— T3 recent_sessions
            # 菜单正确性依赖全历史会话,量级 = 会话数千级 × 几十字节 ≈ 几十 KB
            if len(self._gpart_last) > len(seen) + 64:
                self._gpart_last = {k: v for k, v in self._gpart_last.items()
                                    if k in seen}
            if len(self._sid_text_rowid) > len(seen) + 64:
                self._sid_text_rowid = {k: v for k, v in self._sid_text_rowid.items()
                                        if k in seen}
            return tps
        finally:
            con.close()

    def _rebuild_part_baselines(self, now: float, con) -> bool:
        """part 侧基线全量重建(_global_part_tps 首拍/删顶行·VACUUM 回退时
        独占调用;连接由调用方按单连接纪律开与关,本函数只查不关)。两个
        重建都是本函数职责(B2,T3 依赖):
        - _gpart_last/_sid_text_rowid:旧 4000 行窗口 SQL 一次重建(text 行
          长度基线;窗口 SQL 仅在回退/首拍跑,~46-81ms 不进常态路径);
        - _session_last_rowid:全量 GROUP BY(session_id, MAX(rowid)) 重建
          (任何类型行的每会话最大 rowid;实测 ~42-56ms,同样仅此路径)。
        全有或全无:两查询都成功才落盘并返回 True(调用方据此推进水位),
        任一失败返回 False 且不动现状 —— 半截重建会让 _session_last_rowid
        永久缺会话(走读只覆盖新行,再无补救窗口),整体重试才是安全侧。"""
        try:
            rows = con.execute(
                "SELECT session_id, MAX(rowid), length(data) FROM part"
                " WHERE data LIKE '{\"type\":\"text\"%'"
                " AND rowid > (SELECT MAX(rowid) FROM part) - 4000"
                " GROUP BY session_id").fetchall()
        except sqlite3.Error:
            return False
        try:
            srows = con.execute(
                "SELECT session_id, MAX(rowid) FROM part"
                " GROUP BY session_id").fetchall()
        except sqlite3.Error:
            return False
        self._gpart_last = {}
        self._sid_text_rowid = {}
        for sid, mx_rid, ln in rows:
            if not sid or not ln:
                continue
            self._gpart_last[sid] = (ln, now)
            self._sid_text_rowid[sid] = mx_rid
        # T3 锁序(N3):本函数经 _global_part_tps←_poll_stats 持 snap_lock,
        # 此处再取 _session_lock = snap_lock→_session_lock 唯一合法顺序;
        # 菜单读侧(recent_sessions)只取 _session_lock,绝无反向持锁路径
        with self._session_lock:
            self._session_last_rowid = {sid: rid for sid, rid in srows if sid}
        return True

    def _refresh_completed_rows(self, cut_ms: int) -> None:
        """completed 行缓存整批换行(T2 取数/加权拆分的取数侧)。

        与主查询同一 started_at 前置换行:`started_at>=cut-COMPLETED_
        LOOKBACK_MS AND completed_at>=cut` —— started_at 前缀命中
        model_usage_started_model_idx(真实库实测 0.213ms/SEARCH,同口径复测
        旧 completed_at-only 形态 40.016ms 全表 SCAN;spec 两轮基线 39.7/
        33.71ms,字面照搬当缓存刷新用实测 41.37ms/次,均已否决)。
        completed_at>=cut 只是取数期的行集
        裁剪:加权侧用当前 cut 重算重叠,缓存里 completed_at 已滑出窗的行
        在 ov_ms<=0 处自然排除,故缓存行集偏多无害、偏少(闸门关闭期新完成
        的行)待下拍开闸补齐。

        sqlite3.Error:函数内吞掉、清空缓存并照常盖章 cut —— 该拍完成贡献
        0.0(与旧『查询失败→0.0』同语义),绝不穿透到 _poll_stats(缺列库/
        坏库不炸尾段采样);con.close 置 finally,查询抛错也不泄漏连接。"""
        try:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT output_tokens, first_token_at, completed_at"
                    " FROM model_usage WHERE status='completed'"
                    " AND started_at>=? AND completed_at>=?"
                    " AND output_tokens>0 AND first_token_at IS NOT NULL",
                    (cut_ms - self.COMPLETED_LOOKBACK_MS, cut_ms)).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            self._completed_rows = []
            self._completed_rows_cut_ms = cut_ms
            return
        self._completed_rows = rows
        self._completed_rows_cut_ms = cut_ms

    def _global_completed_tps(self, now: float, window_s: float) -> float:
        """完成贡献:completed 行的输出区间 [first_token_at, completed_at] 与
        最近 window_s 窗口的重叠加权 —— 贡献 = r_i × 重叠秒 / 窗口,其中
        r_i = output/(c−ft)。token 摊到真实生成的那段时间上,完成瞬间不产生
        尖峰;流式请求完成后由 part 增长无缝切换到本项(不重不漏)。
        加权循环与拆分前逐字同式(窗口/加权语义一字不动)。

        T2 取数与加权拆分:行集来自 _completed_rows 缓存(由 _poll_stats 每拍
        调 _refresh_completed_rows 换行;本函数仅在从未取数时兜底取一次),
        每拍用缓存+当前 now 重算重叠 —— T4 闸门关闭期缓存冻结、衰减照推:
        冻结 db 下行集不变,本式与旧『每秒重查』逐拍全等;期间 db 里被删的行
        最多残留贡献 window_s(10s)即自然出窗,容忍。设计边界(B3'):
        started_at<cut-COMPLETED_LOOKBACK_MS 且 completed_at>=cut 的行(单次
        请求持续>2h)在取数侧被排除 —— 本库实测 0 行超 2h(常量注释即声明),
        旧 completed_at-only 形态会计入,属有意收窄而非回归。
        异常语义:sqlite3.Error → 0.0(取数侧自吞并清缓存,见上)。"""
        cut_ms = int((now - window_s) * 1000)
        if self._completed_rows_cut_ms is None:   # 从未取数:直调兜底取一次
            self._refresh_completed_rows(cut_ms)
        win = max(window_s, 1e-3)
        now_ms = now * 1000.0
        tps = 0.0
        for out_tok, ft, c in self._completed_rows:
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
    def _rebuild_session_last_rowid(self) -> None:
        """_session_last_rowid 全量重建(sid→每会话 part 最大 rowid)。

        刻意不过滤 subagent/dwf —— 排除红线是菜单读侧(recent_sessions)的
        职责,与 T1 走读『任何新行都喂』的 append 语义保持同一不变式。调用方
        是 recent_sessions 的冷缓存回填(UI 线程);T1 侧首拍/删顶行回退的全
        量重建在 _rebuild_part_baselines 内自带同形 GROUP BY(那里还要重建
        _gpart_last/_sid_text_rowid 吞吐基线,从菜单路径调它会跨线程踩踏
        引擎线程独占状态,故两处刻意不合并)。
        锁(N3):只取 _session_lock、绝不取 snap_lock —— 菜单线程严禁碰
        引擎主锁;sqlite3.Error 自吞:坏库 → dict 维持原值(重建失败不丢
        已有缓存,菜单下一拍照常读旧值)。"""
        try:
            con = connect_ro()
            try:
                rows = con.execute(
                    "SELECT session_id, MAX(rowid) FROM part"
                    " GROUP BY session_id").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return
        fresh = {sid: rid for sid, rid in rows if sid}
        with self._session_lock:
            self._session_last_rowid = fresh

    def _recent_sessions_sql(self, limit: int = 8) -> list:
        """recent_sessions 的冷缓存回退:改前实现逐字保留。

        真实库实测 67.8/54.88ms(UI 线程对 part 全覆盖索引扫描 + TEMP B-tree
        排序,无行数窗口、随 part 只增不减线性恶化)—— 正是被缓存读替掉的
        形态:DESC LIMIT 窗口对深埋 90,184 行的会话无解、窗口 GROUP BY 版亦
        40.6-58.9ms 不根治(见 registry open 项)。仅缓存冷时走(每引擎至多
        一拍),行为与改前逐字一致就是回退语义的全部要求。"""
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

    def recent_sessions(self, limit: int = 8) -> list:
        """最近会话(手动切换菜单用):按 part 每会话最新写入倒序、排除
        subagent/dwf,与 _latest_session 跟随口径同源。签名/返回形状不变
        (app.py _add_session_menu 的 (sid, title) 消费零改动)。

        v0.9 T3 缓存读:数据源 = 引擎维护的 _session_last_rowid(T1 水位走读
        每秒增量 + 首拍/删顶行回退的全量重建)。启动基线由 run() 序幕的
        _poll_stats() 首拍完成(T1 落地后重建属引擎线程首拍职责,早于三子
        线程启动;与 T3 步骤文本『run() 起步 GROUP BY』的差:同一 GROUP BY
        不必跑两遍 —— 若 _poll_stats 前段查询坏库吞错,菜单首开仍走本方法
        的冷回退+回填自愈,不依赖引擎循环活着)。菜单只做快照拷贝 + Python
        过滤 + title 批查(session.id 主键 IN 点查,暖连接实测 0.008ms),暖读
        本机实测 ~0.7-0.9ms(改前 67.8/54.88ms;残余大头=每查询独立只读连接
        的首条语句 ~0.64ms —— SQLite 惰性 schema/页加载,全部 fetch_* 同款
        纪律、刻意不为菜单破例缓存连接:驻留句柄会挡 ZCode 自己的库维护)。
        语义与旧 SQL 逐字等价:sess_subagent*/sess_dwf-* 排除红线(Python
        startswith,与 _handle_log_line 同款约定);批查缺行/NULL 标题 →
        空标题而非丢会话(旧 LEFT JOIN);(title or '').strip()[:16] 截断
        不变;rowid 倒序 + LIMIT 逐字镜像 SQLite(负 int=不设上限、0=空;
        None/不可绑定值旧路径在 execute 即抛 sqlite3.Error 被吞成 [] ——
        实测 LIMIT NULL → IntegrityError datatype mismatch,同判)。
        缓存冷(空 dict:引擎未 start/run 序幕未及首拍/首拍失败/part 空)→
        回退跑一次 _recent_sessions_sql 并全量回填,下一拍起暖读;回填失败
        (库又没了)只意味着下一拍仍走回退,结果始终与改前一致。
        锁序(N3):本方法只取 _session_lock、绝不取 snap_lock —— 菜单在
        UI 线程,不得与引擎每秒 tick 争引擎主锁;引擎写侧持 snap_lock 时取
        _session_lock 是唯一合法顺序,严禁反向持锁。"""
        # LIMIT 语义镜像(见 docstring):先于快照判定,非法输入不触库
        if limit is None:
            return []
        try:
            n = int(limit)
        except (TypeError, ValueError):
            return []
        with self._session_lock:
            snap = dict(self._session_last_rowid)
        if not snap:
            # 冷缓存:回退现 SQL(逐字保留)+ 全量回填。回填不看本拍结果:
            # part 空表回填得空 dict(下一拍仍冷仍回退,旧代价)、库缺失回填
            # 自吞失败 —— 两条路径都不产生错误结果,只是慢
            rows = self._recent_sessions_sql(limit)
            self._rebuild_session_last_rowid()
            return rows
        # Python 过滤(与旧 SQL 的 NOT LIKE 同红线;走读/重建写入侧 `if sid`
        # 已挡掉 NULL/空串,这里双保险同判)
        items = [(rid, sid) for sid, rid in snap.items()
                 if sid and not sid.startswith(("sess_subagent", "sess_dwf-"))]
        items.sort(key=lambda t: t[0], reverse=True)   # rowid 倒序;每会话最大
        # rowid 互异无并列,序确定
        if n >= 0:
            items = items[:n]
        sids = [sid for _rid, sid in items]
        if not sids:
            return []
        titles = {}
        try:
            con = connect_ro()
            try:
                # IN 点查(session.id 主键);字符串只拼 '?' 个数、值全部参数
                # 绑定;分块防 SQLite 变量数上限(菜单恒传 8,纯防御)
                for i in range(0, len(sids), 400):
                    chunk = sids[i:i + 400]
                    q = ("SELECT id, title FROM session WHERE id IN (%s)"
                         % ",".join("?" * len(chunk)))
                    for sid, t in con.execute(q, chunk).fetchall():
                        titles[sid] = t or ""
            finally:
                con.close()
        except sqlite3.Error:
            # 旧实现是单条 SQL:session 表缺失/坏库时整体失败 → [];批查
            # 失败同判(会话列表与标题一体可得不可得,与旧 LEFT JOIN 一致)
            return []
        return [(sid, titles.get(sid, "").strip()[:16]) for sid in sids]

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

    # ---- Claude watcher(v0.9 T6,Live 实时刷新:watchdog 可选加速器) ----
    # 红线:watcher 的定义与启动都在 data_engine —— sources 包严禁 import
    # data_engine(v0.6.0 双模块实例事故),所以『FS 事件 → 引擎』的桥只能
    # 由引擎侧自建:本线程监听 ClaudeSource.projects_dir(默认
    # ~/.claude/projects)的 .jsonl 变更 → ClaudeSource.note_changes(T5 的
    # 增量入口)→ self._wake.set()(T4 的引擎唤醒事件)让 _db_loop 提前
    # 出数。加速器而非承重结构:任何死亡/缺位(watchdog 未安装、目录不
    # 存在、启动失败、循环体异常)都只回退 ClaudeSource.SCAN_TTL=15s 的
    # 既有节奏 —— 数据永不错只是慢,引擎三循环与 quota 调度照跑(P1 家族
    # fixed4/fixed7/10/11 的结构性防御:绝不给 watcher 新增承重路径)。

    def _claude_source(self):
        """引擎源列表里的 ClaudeSource 实例(无则 None)。watcher 只服务
        Claude 行的提前出数:其他源没有 note_changes 增量入口,监听无意义;
        isinstance 而非按 name 匹配 —— 未来的 ClaudeSource 子类自然继承。"""
        for s in self.sources:
            if isinstance(s, ClaudeSource):
                return s
        return None

    def _claude_watch_offer(self, path) -> None:
        """watchdog handler 的唯一动作:.jsonl 路径入队,其余(目录事件、
        别的扩展、缺 src_path 的怪事件)直接丢弃 —— 过滤口径与
        ClaudeSource._scan 的 .jsonl 判定同款,勿比源更宽:多入队的路径
        只会让 note_changes 白跑。handler 只入队绝不解析:watchdog 的
        emitter 线程直接回调本方法,在这里读 jsonl 会阻塞它、拖慢全部
        事件分发;真正的解析全在引擎侧 _claude_watch_pump 做。"""
        if isinstance(path, str) and path.endswith(".jsonl"):
            self._claude_watch_q.put(path)

    def _claude_watch_drain(self) -> list:
        """非阻塞清空待处理队列:防抖窗内到达的事件并成一批(顺序保留)。"""
        out = []
        while True:
            try:
                out.append(self._claude_watch_q.get_nowait())
            except queue.Empty:
                return out

    def _claude_note_changed(self, paths) -> None:
        """一批 .jsonl 变更 → 源增量解析 + 唤醒引擎(watcher 主循环每轮的
        收口)。顺序钉死『先 note_changes 后置位 _wake』:被唤醒的 _db_loop
        轮询 today_by_source 时源缓存已更新,提前出数才有意义。两个依赖
        都按『有则用』消费:getattr 拿 _wake —— T4 未落地/注入式单测环境
        可能没有该事件,此时 note_changes 仍已完成,下一拍 TTL 路径照常
        出数(watcher 只提前、不承重);异常不在本层吞 —— 循环体的
        try/except Exception 在 _claude_watch_loop 收口(死亡=回退 15s TTL)。
        刻意不取 snap_lock:watcher 线程若与闸门/会话切换共享引擎锁,就把
        『加速器』变成了潜在的死锁源(P1 家族防御),它只应触碰源与 _wake。"""
        src = self._claude_source()
        if src is None:
            return
        # 去重保序:watchdog 对一次 append 常连发多条 modified,同文件重复
        # 入队是常态;note_changes 对同文件幂等(同 offset 无新行),去重
        # 只是省一遍 IO
        uniq = list(dict.fromkeys(paths))
        src.note_changes(uniq)
        wake = getattr(self, "_wake", None)
        if wake is not None:
            wake.set()

    def _claude_watch_pump(self) -> bool:
        """watcher 主循环的一轮:阻塞等首个事件(0.5s 超时只为周期性回查
        stop_flag,不是节流)→ WATCH_DEBOUNCE_S 防抖窗内合并 → note_changes
        + _wake 置位。返回本轮是否分发过事件。单独成方法的唯一目的:注入式
        单测直接向 _claude_watch_q 喂合成事件即可驱动完整分发路径 ——
        不依赖 watchdog 安装、不发真实 FS 事件(验收钉死的测试口径)。"""
        try:
            first = self._claude_watch_q.get(timeout=0.5)
        except queue.Empty:
            return False
        # 合并防抖:首事件后再等一窗,把同批突发并成一轮 note_changes
        time.sleep(self.WATCH_DEBOUNCE_S)
        paths = [first]
        paths.extend(self._claude_watch_drain())
        self._claude_note_changed(paths)
        return True

    def _claude_watch_loop(self):
        """Claude jsonl watcher(daemon 线程,run() 与 _tail/_db/_part 同批
        启动):watchdog 递归监听 projects 目录,.jsonl 事件入队 → 100ms
        防抖合并 → note_changes 增量解析 → _wake 置位。

        每道失败路径都是『dbg + return』且不重试(缺依赖每秒重试 import
        只会刷屏;回退 TTL 后重启应用即恢复):
        - 守卫环境(--verify/ZM_NO_STATE,与 QuotaMonitor 同纪律):回归
          测试绝不装真实 ~/.claude 的 FS 监听,测试行为不随机器漂移;
        - 无 Claude 源 / 无 note_changes 增量入口(T5 未落地的分批合并
          窗口期)/ projects 目录不存在:无事可做,静默回退 15s TTL;
        - watchdog 未安装(可选依赖,exe 默认不打包):import 收在函数内
          —— 挂在模块顶层会让缺依赖的机器启动即崩;ImportError 之外,
          坏安装的任意导入异常同样降级(缺依赖=慢不死);
        - observer 启动失败 / 循环体异常:线程退出,引擎三循环照跑。
        """
        if _no_persist():
            return
        src = self._claude_source()
        if src is None:
            dbg("claude watcher: no Claude source, fallback to 15s TTL")
            return
        if getattr(src, "note_changes", None) is None:
            dbg("claude watcher: note_changes unavailable,"
                " fallback to 15s TTL")
            return
        proj = getattr(src, "projects_dir", None)
        if not proj or not os.path.isdir(proj):
            dbg("claude watcher: projects dir unavailable,"
                " fallback to 15s TTL")
            return
        try:
            from watchdog.observers import Observer       # 函数内 import:可选依赖
            from watchdog.events import FileSystemEventHandler
        except Exception:              # ImportError=缺依赖;坏安装的异常同降级
            dbg("claude watcher unavailable, fallback to 15s TTL")
            return
        eng = self

        class _JsonlHandler(FileSystemEventHandler):
            """只入队(on_any_event 收全部事件类型;『绝不解析』红线见
            _claude_watch_offer docstring)。"""

            def on_any_event(self, event):
                eng._claude_watch_offer(getattr(event, "src_path", None))

        try:
            obs = Observer()
            obs.schedule(_JsonlHandler(), proj, recursive=True)
            obs.start()
        except Exception as exc:
            dbg(f"claude watcher start failed: {type(exc).__name__},"
                " fallback to 15s TTL")
            return
        try:
            while not self.stop_flag.is_set():
                self._claude_watch_pump()
        except Exception as exc:
            dbg(f"claude watcher died: {type(exc).__name__}: {exc};"
                " fallback to 15s TTL")
        finally:
            try:
                obs.stop()
                obs.join(timeout=2.0)
            except Exception:
                pass

    # ---- 主循环 ----
    def run(self):
        # P1 修复:session_id 为空(全新机器先装 meter 后才用 zcode、启动时
        # DB 缺失/被写锁超 2s/part 表空)绝不能直接 return —— 那会连三个轮询
        # 线程都不起,本线程永久死亡且无重试无看门狗,整卡从此零数据、无报错,
        # 唯一恢复方式是重启应用(线程二度 start 直接 RuntimeError)。空 id 下
        # 会话级查询自然空转(session_id='' 匹配不到行,今日用量等全局字段
        # 照常出数),主循环每 0.5s 的 _refresh_session 会在首个可识别会话
        # 出现时经 _switch_session 完成自愈 —— 前提是循环活着,这就是本行
        # 存在的意义。
        self.snap.title = self._session_title()
        self._poll_stats()
        self._init_tps_for_session()
        self._push()
        for target in (self._tail_loop, self._db_loop, self._part_loop):
            threading.Thread(target=target, daemon=True).start()
        # T6:Claude watcher(watchdog 可选加速器;死亡/缺位=回退 15s TTL,
        # 引擎三循环照跑)。守卫环境(--verify/ZM_NO_STATE,与 QuotaMonitor
        # 同纪律)不启动:回归测试绝不装真实 ~/.claude 的 FS 监听;循环内
        # 首行还有同款守卫,兜住不经 run() 的直接调用(注入式单测)。命名
        # 线程便于区分:三循环刻意匿名(现状不动),新线程有名字才可观测。
        if not _no_persist():
            threading.Thread(target=self._claude_watch_loop, daemon=True,
                             name="zm-claude-watch").start()
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

    def _volatile_fp(self) -> tuple:
        """变化即 push 的『易变字段』指纹:闸门管辖段 + 尾段 + _poll_new_
        completed 会写的全部数据字段(新完成请求 → tps_exact 家族有变 →
        push,数字尽快落地)。刻意不含 recent_speeds —— 它随 _gtps_hist
        每 tick 追加恒有变,纳入会把『值未变不 push』判空;也不含 state/
        title/gen_elapsed/manual 等事件路径字段(run() 主循环与
        _switch_session 自带 push,note4)。speed_by_model 行含 None,行
        元组化后可直接比较。"""
        s = self.snap
        return (s.global_tps, s.today_tokens, s.today_cost_cny,
                s.today_cost_partial, s.burn_tokens_per_hour,
                s.burn_avg_tokens_per_hour, s.burn_instant_per_hour,
                s.burn_cny_per_hour, s.est_hours_left, s.session_in,
                s.session_cache, s.session_out, s.cache_rate, s.tps_avg,
                s.model, s.tps_exact, s.last_ttft, s.last_duration,
                tuple(s.today_by_source or ()),
                tuple(tuple(r) for r in (s.speed_by_model or ())))

    def _push(self):
        self.snap.updated = time.time()
        try:
            self.out.put_nowait(self.snap)
        except queue.Full:
            pass

    def stop(self):
        self.stop_flag.set()
        # T4:双 set —— 唤醒可能正阻塞在 _wake.wait(POLL_DB) 的 _db_loop,
        # 停机不必等满一个轮询周期(stop_flag 由各循环自行判定)。
        self._wake.set()


