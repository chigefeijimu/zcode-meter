"""Claude Code 用量源:~/.claude/projects/**/*.jsonl 只读解析。

类与 _claude_ts_local 自 data_engine 原样迁移,行为与 SQL/解析规则
一字未动(test_claude_source_synthetic 钉死)。

红线:严禁在本包内 import data_engine(见 base.py 模块 docstring)。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

from .base import UsageSource


def _claude_ts_local(s):
    """Claude jsonl 的 timestamp(ISO-UTC,如 2026-07-21T02:33:00.699Z)→
    本地时区 aware datetime;坏值返回 None。天界必须转本地再定,直接取
    UTC 日期会把本地 0 点前的用量算进前一天(CN 时区恒差 8 小时)。"""
    if not isinstance(s, str) or not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))  # py3.10 不认 Z
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)               # 裸时间按 UTC
        return d.astimezone()
    except ValueError:
        return None


class ClaudeSource(UsageSource):
    """Claude Code 源:只读解析 ~/.claude/projects/**/*.jsonl。
    口径(实测 1921 条 assistant 行验证):
    - 只取 type=='assistant' 行的 message.usage;含 isSidechain 行
      (与 ZCode 今日用量含 subagent 对齐);
    - in = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
      —— Anthropic 口径 input_tokens 不含 cache,ZCode 口径已含,补齐后两源
      才可比(README 口径表已写明,两源合计口径不同勿当 bug);
    - 按 message.id 全局去重,但 isApiErrorMessage 行与 usage 全零行先跳过
      再去重 —— keep-last 会让后到的零用量 error 行清零真实用量(实测存在);
    - 解析结果按 (path, mtime, size) 缓存;另有 15s 扫描 TTL —— 引擎 1s 一轮
      的 _poll_stats 也调它,不节流会把 jsonl 目录扫成热点。"""

    name = "Claude"
    # discover_sources 排序键:10 = 排在 ZCode(0)之后,与迁移前
    # [ZCodeSource(), ClaudeSource()] 列表顺序一致(卡片显示顺序不变)
    order = 10
    SCAN_TTL = 15.0
    # 文件级缓存放类属性:单测一轮会 new 多个 DataEngine(每个带一个源实例),
    # 共享缓存避免把同一批 jsonl 反复解析;键含 (mtime,size) 保证不读过期内容,
    # 条目解析后只读,跨实例共享安全(GIL 下最坏重复解析一次,结果相同)。
    _file_cache: dict = {}              # path -> ((mtime, size), [(ts,in,out,mid)])

    def __init__(self, projects_dir: str | None = None):
        self.projects_dir = projects_dir or os.path.expanduser("~/.claude/projects")
        self._entries: list | None = None
        self._scanned_at = 0.0

    def is_available(self) -> bool:
        return os.path.isdir(self.projects_dir)

    def _parse_file(self, path: str) -> list:
        """单文件 → [(ts_local, in_tok, out_tok, message_id)]。跳过规则
        (isApiErrorMessage / usage 全零)在这里做,先于全局去重 —— 这是
        「keep-last 会清零真实用量」教训的钉死顺序。"""
        out = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "assistant":
                        continue
                    if obj.get("isApiErrorMessage"):
                        continue
                    msg = obj.get("message")
                    usage = msg.get("usage") if isinstance(msg, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    vals = {}
                    for k in ("input_tokens", "cache_read_input_tokens",
                              "cache_creation_input_tokens", "output_tokens"):
                        v = usage.get(k)
                        vals[k] = v if isinstance(v, int) and v > 0 else 0
                    if sum(vals.values()) == 0:
                        continue                      # 零用量行(残留 error 等)
                    ts = _claude_ts_local(obj.get("timestamp"))
                    if ts is None:
                        continue
                    mid = msg.get("id")
                    out.append((ts,
                                vals["input_tokens"] + vals["cache_read_input_tokens"]
                                + vals["cache_creation_input_tokens"],
                                vals["output_tokens"],
                                mid if isinstance(mid, str) else None))
        except OSError:
            return []
        return out

    def _scan(self) -> list:
        now = time.time()
        if self._entries is not None and now - self._scanned_at < self.SCAN_TTL:
            return self._entries
        seen, entries = set(), []
        for root, _dirs, files in os.walk(self.projects_dir):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                p = os.path.join(root, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                key = (st.st_mtime, st.st_size)
                cached = self._file_cache.get(p)
                if cached is not None and cached[0] == key:
                    per_file = cached[1]
                else:
                    per_file = self._parse_file(p)
                    self._file_cache[p] = (key, per_file)
                for ts, i, o, mid in per_file:
                    # message.id 去重跨文件全局做(流式响应同 id 多行只计一次);
                    # 无 id 的行不参与去重(无法识别身份,宁多勿漏)
                    if mid is not None:
                        if mid in seen:
                            continue
                        seen.add(mid)
                    entries.append((ts, i, o))
        self._entries = entries
        self._scanned_at = now
        return entries

    def today_usage(self) -> int:
        if not self.is_available():
            return 0
        today = dt.date.today()
        return sum(i + o for ts, i, o in self._scan() if ts.date() == today)

    def daily_usage(self, days: int = 30) -> list:
        if not self.is_available():
            return []
        lo = dt.date.today() - dt.timedelta(days=days - 1)
        agg: dict = {}
        for ts, i, o in self._scan():
            d = ts.date()
            if d < lo:
                continue
            agg[d.isoformat()] = agg.get(d.isoformat(), 0) + i + o
        return sorted(agg.items())


# discover_sources() 约定:模块级 Source 属性指向本模块的源类(README
# 『如何贡献一个源』)—— 类名本身随意,发现机制只认 Source 这个名字
Source = ClaudeSource
