"""Claude Code 用量源:~/.claude/projects/**/*.jsonl 只读解析。

类与 _claude_ts_local 自 data_engine 原样迁移;v1 起增量偏移解析
(registry#19):文件缓存条目 {mtime_ns,size,offset,entries,seen_mid,
date_agg},追加只 seek(offset) 解析新增字节 —— 单文件成本从
O(文件大小) 的整文件重解析降为 O(新增字节)。解析/跳过/补齐规则与
去重顺序一字未动(test_claude_source_synthetic 钉死)。

红线:严禁在本包内 import data_engine(见 base.py 模块 docstring);
严禁写任何 jsonl(本模块对 jsonl 只读,watcher 事件亦只触发重读)。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
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
    except (ValueError, OSError, OverflowError):
        # 『坏值返回 None』是本函数的契约,但 astimezone 的失败面不止
        # ValueError:边界日期(如 9999-12-31)在 Windows CRT localtime_s
        # 抛 OSError [Errno 22],POSIX+东八区下 fromutc 溢出抛 OverflowError
        # —— 两者都不是 ValueError 子类,一旦穿透,_parse_line 的行级
        # 跳过失效,异常会沿 _scan→today_usage→_poll_stats 一路上抛
        # (那里只捕 sqlite3.Error)杀掉引擎线程,且增量实现里该行已
        # 被消费、不会重试(2026-09-30 P1 实测:一行 9999 时间戳 →
        # 整个 jsonl 今日用量归 0,即同一教训的文件级形态)。
        return None


def _norm_path(p: str) -> str:
    """缓存键归一。os.walk(expanduser 产出的混合分隔符路径)与 watchdog
    事件路径的字符串形态可能不同;normpath 统一分隔符、normcase 统一
    大小写(Windows 文件系统不区分),两路来源落到同一条目 —— 键不一致
    等于同文件双条目,双条目就是双计。"""
    return os.path.normcase(os.path.normpath(p))


class ClaudeSource(UsageSource):
    """Claude Code 源:只读解析 ~/.claude/projects/**/*.jsonl。
    口径(实测 1921 条 assistant 行验证,自迁移起一字未动):
    - 只取 type=='assistant' 行的 message.usage;含 isSidechain 行
      (与 ZCode 今日用量含 subagent 对齐);
    - in = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
      —— Anthropic 口径 input_tokens 不含 cache,ZCode 口径已含,补齐后两源
      才可比(README 口径表已写明,两源合计口径不同勿当 bug);
    - 按 message.id 全局去重(跨文件,作用域=本源目录树,见 _mid_owner),
      但 isApiErrorMessage 行与 usage 全零行先跳过再去重 —— keep-last 会让
      后到的零用量 error 行清零真实用量(实测存在);
    - 增量偏移解析(registry#19):每文件条目记 {mtime_ns,size,offset,…},
      stat 未变短路、增长只尾读新增字节 O(新增字节),截断/原地重写才
      整文件重解析 + 确定性重建;另有 15s 扫描 TTL —— 引擎 1s 一轮的
      _poll_stats 也调它,不节流会把 jsonl 目录扫成热点。watcher(T6,
      data_engine 侧线程)活体时走 note_changes 摄取并失效 TTL;
      watcher 死亡/缺位时 TTL 周期的 walk+stat 兜底 —— 数据永不错,
      只是慢到 TTL。"""

    name = "Claude"
    # discover_sources 排序键:10 = 排在 ZCode(0)之后,与迁移前
    # [ZCodeSource(), ClaudeSource()] 列表顺序一致(卡片显示顺序不变)
    order = 10
    SCAN_TTL = 15.0
    # 类级锁:缓存是类级共享的(单测一轮会 new 多个 DataEngine,每个带
    # 一个源实例,共享缓存避免把同一批 jsonl 反复解析),按实例加锁锁不住
    # 跨实例访问。_file_cache/_mid_owner/_scope_files 的全部读写、实例
    # TTL 字段的失效与写回都在锁内;文件读取(含 seek 尾读)也在锁内 ——
    # 解析在锁内串行化的代价可接受:生产单实例,冷启动一次性(本机基线
    # 6 jsonl/11.4MB 实测 ~100ms 量级),此后尾读 O(新增字节),换来
    # watcher 线程 note_changes 与引擎线程 _scan 的零竞态,无需双检/重试。
    _lock = threading.Lock()
    # 条目六字段:{mtime_ns,size}=上次摄取时的 stat(两者都未变即短路,
    # mtime_ns 比 st_mtime 多 100ns 位,原地重写检测靠它);offset=已
    # 消费字节数(只越过完整 \n 行);entries=该文件全部合格行(整文件
    # 重解析重建归属的原料,含被抑制行 —— 重建要按完整行集重导,不能
    # 只留归属行);seen_mid=本文件 claim 到的 mid;date_agg=本文件
    # 归属行的 按本地天 in+out 之和。
    _file_cache: dict = {}   # normpath -> {mtime_ns,size,offset,entries,seen_mid,date_agg}
    # 全局 mid→owner_path 所有权映射(N1 裁决):tail 追加时首 claim
    # 者胜、后来者抑制 —— 与迁移前『每次 walk 一个 seen 集合、先到先得』
    # 同语义,只是把 seen 常驻化以支撑跨轮增量。按 projects_dir 作用域
    # 分桶:去重边界=同目录树跨文件,与迁移前 per-walk seen 完全一致,
    # 测试临时目录/多实例互不串扰。
    _mid_owner: dict = {}    # scope -> {mid: owner_normpath}
    # 每个 scope 上一次 walk 摘到的文件键集:仅用于删除/改名检测 ——
    # 缓存过的键从 walk 中消失 ⇒ 旧贡献作废,触发一次 sorted 序重建。
    # 不做这步的话,被删文件独占过的 mid 会永远压着幸存文件的同名行,
    # 而迁移前的『每次 walk 重建 seen』在删除后会重新计入 —— 语义必须
    # 对齐。(条目本身的只增不删是另一问题,registry open#18,本批不修。)
    _scope_files: dict = {}  # scope -> set(normpath)

    def __init__(self, projects_dir: str | None = None):
        self.projects_dir = projects_dir or os.path.expanduser("~/.claude/projects")
        # TTL 缓存的聚合视图 {date_iso: in+out};None=失效,下一轮重建。
        # 聚合 = 各文件 date_agg 求和 O(文件数),不再持有全局去重行列表
        # (date_agg 在摄取时已按归属算好,today/daily 只做查表)。
        self._entries: dict | None = None
        self._scanned_at = 0.0

    def is_available(self) -> bool:
        return os.path.isdir(self.projects_dir)

    def _scope(self) -> str:
        """所有权作用域键(归一化 projects_dir)。"""
        return _norm_path(self.projects_dir)

    @staticmethod
    def _parse_line(line: str):
        """单行文本 → (ts_local, in_tok, out_tok, mid) | None。
        跳过规则(isApiErrorMessage / usage 全零)在这里做,先于全局
        去重 —— 这是「keep-last 会清零真实用量」教训的钉死顺序;判定
        与迁移前 _parse_file 的整文件行迭代逐字相同,拆成逐行是为让
        初始全量与增量尾读共用同一份规则(两路解析规则漂移比慢更危险)。
        文本态按 \n 分行与迁移前一致:\n 不可能是多字节 UTF-8 序列的
        组成字节,行边界在字节态与文本态完全重合。"""
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            return None
        if obj.get("isApiErrorMessage"):
            return None
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            return None
        vals = {}
        for k in ("input_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "output_tokens"):
            v = usage.get(k)
            vals[k] = v if isinstance(v, int) and v > 0 else 0
        if sum(vals.values()) == 0:
            return None                      # 零用量行(残留 error 等)
        ts = _claude_ts_local(obj.get("timestamp"))
        if ts is None:
            return None
        mid = msg.get("id")
        return (ts,
                vals["input_tokens"] + vals["cache_read_input_tokens"]
                + vals["cache_creation_input_tokens"],
                vals["output_tokens"],
                mid if isinstance(mid, str) else None)

    @staticmethod
    def _read_new_rows(path: str, offset: int):
        """从 offset 读到 EOF → (合格行列表, 新offset);OSError → None。
        行完整性以 \n 为准:未终止的尾部残行不解析、新 offset 不越过它
        (留在残行起点,下次尾读从残行起点读到 EOF,自然把补全后的整行
        一次计入)。若按残行先计入、补全后再整行重读,会开出『同一行计
        两次』的窗口(v0.2.0 input 双计教训);jsonl 追加以完整行为单位,
        残行只可能是写了一半 —— 本机实测 6 个真实 jsonl 全部 \n 收尾,
        静态缺尾换行(写者崩溃在 JSON 边界)的极端情况接受延迟到补全
        或整文件重解析,绝不吃双计。"""
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            return None
        rows = []
        parts = chunk.split(b"\n")
        partial = parts.pop()          # 末段无 \n 即残行(恰在 \n 后则为空)
        for b in parts:
            r = ClaudeSource._parse_line(b.decode("utf-8", "replace"))
            if r is not None:
                rows.append(r)
        # 只越过完整行(各段 + 分隔符)的字节;残行留给下一次尾读
        return rows, offset + len(chunk) - len(partial)

    def _claim_rows(self, scope: str, key: str, e: dict, rows: list) -> None:
        """锁内把新解析的合格行落进条目。全局 mid 首 claim 者胜:后来者
        (哪怕在本文件内重复出现)抑制 —— 与迁移前 walk 级去重同语义;
        无 mid 的行不参与去重(无法识别身份,宁多勿漏)。被抑制的行也进
        entries(重建归属的原料,见 _parse_line 条目注释)。
        note2:纯追加的事件序与整文件重解析的 sorted(path) 序,在同 mid
        跨文件且 usage 不同时归属可能不同 —— 继承现实现 os.walk 序非
        确定的等价风险(极端 fork 场景:同 mid 出现在多个文件且用量
        不一致),声明不改。"""
        owner = self._mid_owner.setdefault(scope, {})
        for ts, i, o, mid in rows:
            e["entries"].append((ts, i, o, mid))
            if mid is not None:
                if mid in owner:
                    continue
                owner[mid] = key
                e["seen_mid"].add(mid)
            d = ts.date().isoformat()
            e["date_agg"][d] = e["date_agg"].get(d, 0) + i + o

    @classmethod
    def _rebuild_scope(cls, scope: str, keys) -> None:
        """锁内确定性重建(N1 裁决):按 sorted(path) 从各文件 entries
        现场重导全局归属与各文件 date_agg。触发条件=任一整文件重解析
        事件(截断/轮转/原地重写)或 walk 发现文件消失 —— 只丢被重解析
        文件的旧条目而不全局重建的话,其他文件里因『mid 已被它 claim』
        而被抑制的行永远不会解禁。换 sorted 序属有意变更:迁移前的重解析
        归属序跟随 os.walk(跨平台/跨次运行不确定),钉死 sorted 才能让
        『重解析结果 == 从零重建』成为可断言的性质。"""
        owner = {}
        for k in sorted(keys):
            e = cls._file_cache.get(k)
            if e is None:
                continue
            e["seen_mid"], e["date_agg"] = set(), {}
            for ts, i, o, mid in e["entries"]:
                if mid is not None:
                    if mid in owner:
                        continue
                    owner[mid] = k
                    e["seen_mid"].add(mid)
                d = ts.date().isoformat()
                e["date_agg"][d] = e["date_agg"].get(d, 0) + i + o
        cls._mid_owner[scope] = owner

    def _ingest(self, scope: str, key: str, st) -> str:
        """锁内单文件摄取(调用方保证持锁、st 为锁内新鲜 stat)。
        返回 'unchanged'/'new'/'append'/'reparse'/'error':
        - unchanged:mtime_ns+size 与上次摄取一致 → O(1) 短路(watcher
          活着时的常态;重复事件/相邻 tick 都落在这里);
        - append:文件增长 → seek(offset) 只解析新增字节;
        - reparse:整文件重解析事件,调用方必须随后重建该 scope(见
          _rebuild_scope 注释,漏重建会让被抑制行永远锁死);
        - error:读失败 → 条目保持上次好值不落盘,下一轮重试。迁移前
          『OSError 把整文件清零并按 (mtime,size) 缓存空结果』会因一次
          瞬时锁文件永久吞掉真实用量,这里改为保守重试。
        重解析判据:任何收缩(size<上次 size,含显式 size<offset 的截断
        /轮转)或『size 不变而 mtime_ns 变』(原地重写)。收缩但未跌破
        offset 同样意味着旧字节区已被改写 —— 当追加读会把新文件的旧字节
        再解析一遍,故一并归入重解析。size 增长时即使内容被整体轮转
        替换,与追加在 stat 层面不可区分,按追加处理(Claude Code 对
        jsonl 只追加,这是既定事实;真轮转必然伴随收缩或原地重写)。"""
        e = self._file_cache.get(key)
        if e is not None and e["mtime_ns"] == st.st_mtime_ns \
                and e["size"] == st.st_size:
            return "unchanged"
        if e is None:
            # 冷路径:整文件首解析一次,此后只 O(新增字节) 尾读
            res = self._read_new_rows(key, 0)
            if res is None:
                return "error"
            rows, off = res
            e = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "offset": off,
                 "entries": [], "seen_mid": set(), "date_agg": {}}
            self._file_cache[key] = e
            self._claim_rows(scope, key, e, rows)
            return "new"
        if st.st_size < e["size"] or (st.st_size == e["size"]
                                      and st.st_mtime_ns != e["mtime_ns"]):
            kind, off0 = "reparse", 0
        else:
            kind, off0 = "append", e["offset"]
        res = self._read_new_rows(key, off0)
        if res is None:
            return "error"
        rows, off = res
        e["mtime_ns"], e["size"], e["offset"] = st.st_mtime_ns, st.st_size, off
        if kind == "reparse":
            # 作废旧贡献:三件套清空换新;归属与 date_agg 交由调用方
            # 紧随的 _rebuild_scope 按确定性序统一重导(见 N1 注释)
            e["entries"], e["seen_mid"], e["date_agg"] = [], set(), {}
        self._claim_rows(scope, key, e, rows)
        return kind

    def _scan(self) -> dict:
        """walk 本目录全部 .jsonl → 聚合 {date_iso: in+out}。15s TTL 只
        节流 os.walk+stat 的目录扫描(watcher 缺位/死亡时的正确性兜底);
        watcher 活着时 note_changes 已把变更摄取进类级缓存并失效 TTL,
        本方法对未变文件是 stat 短路,聚合 = 各文件 date_agg 求和
        O(文件数)。
        瞬时失败与消失的界线(2026-10-02 P1):stat OSError / _ingest
        "error" 只说明『walk 见到但本轮读不动』(AV/备份/写者瞬锁是
        _ingest error 分支的自述动机),不等于文件消失 —— 失败文件的
        last-good 聚合与 mid 归属原样保留;真消失只认 walk 不再列出
        (删除在 walk→锁间隙发生时同样被下一轮兜住)。"""
        now = time.time()
        if self._entries is not None and now - self._scanned_at < self.SCAN_TTL:
            return self._entries
        walked = set()
        for root, _dirs, files in os.walk(self.projects_dir):
            for fn in files:
                if fn.endswith(".jsonl"):
                    walked.add(_norm_path(os.path.join(root, fn)))
        scope = self._scope()
        agg: dict = {}
        with self._lock:
            keys = []
            errored = set()
            reparse = False
            for k in sorted(walked):     # 确定性摄取序,与重建序一致(note2)
                try:
                    st = os.stat(k)      # 锁内新鲜 stat:防 walk→锁间隙的变更
                except OSError:
                    errored.add(k)       # 瞬时读不动:保 last-good,非消失
                    continue
                kind = self._ingest(scope, k, st)
                if kind == "reparse":
                    reparse = True
                if kind == "error":
                    errored.add(k)       # 读失败契约是重试,不是当轮清零
                else:
                    keys.append(k)
            last = self._scope_files.get(scope)
            # 消失判定只认 walk:上轮在册而本轮 walk 未列出才是真消失。
            # 旧判据 not last.issubset(keys) 把瞬时失败也当消失:当轮聚合
            # 丢 last-good 已违反 _ingest 的 error 契约,更糟的是用不含
            # 它的 keys 重建 → 共享 mid 归属翻给幸存文件;它恢复后走
            # unchanged 短路不再重建,同 mid 在两个文件的 date_agg 并存
            # → 今日用量永久双计(input 双计教训家族,2026-10-02 P1)。
            if reparse or (last is not None and not last.issubset(walked)):
                # 重建集并入瞬时失败文件(其条目未动,按完整行集重导
                # 归属)——漏并的话重建本身就制造一次归属翻转
                self._rebuild_scope(scope, set(keys) | errored)
            self._scope_files[scope] = set(keys) | errored
            for k in keys:
                for d, v in self._file_cache[k]["date_agg"].items():
                    agg[d] = agg.get(d, 0) + v
            for k in errored:
                # 兑现 _ingest 的 error 契约:失败轮聚合用 last-good 而非
                # 归零(冷路径失败无缓存条目,get 容缺自然跳过)
                e = self._file_cache.get(k)
                if e is not None:
                    for d, v in e["date_agg"].items():
                        agg[d] = agg.get(d, 0) + v
            # TTL 字段写回也在锁内:与 note_changes 的失效互斥,杜绝
            # 『_scan 算完旧值 → note_changes 失效 → _scan 写回旧值』
            # 的丢失失效竞态(两者的锁内先后 whichever,顺序始终成立)
            self._scanned_at = now
            self._entries = agg
        return agg

    def note_changes(self, paths) -> None:
        """watcher 线程入口(T6 Claude watcher):对报告的 .jsonl 路径做
        增量摄取(seek(offset) 只解析新增字节),随后失效本实例 TTL ——
        下一次 today_usage/daily_usage 立即重新 walk+聚合而不是等
        SCAN_TTL,watch→wake→push 的 Live 链路就靠这一步出新鲜数。
        只处理位于本源 projects_dir 之内的路径(watcher 只注册该子树,
        防御外来源);删除/改名事件(stat 失败)摄取无从谈起,但同样
        失效 TTL,让下一轮 walk 的 vanished 检测立即裁决。重解析事件
        波及全局归属,按 sorted(path) 重建整个 scope(N1)。"""
        scope = self._scope()
        prefix = scope + os.sep
        norm = set()
        for p in paths:
            p = os.fspath(p)
            if p.endswith(".jsonl"):
                norm.add(_norm_path(p))
        if not norm:
            return
        with self._lock:
            touched = reparse = False
            for k in sorted(norm):       # 确定性序:同批多路径的去重顺序可复现
                if not k.startswith(prefix):
                    continue
                try:
                    st = os.stat(k)
                except OSError:
                    touched = True        # 删除/改名:失效 TTL,walk 侧裁决
                    continue
                kind = self._ingest(scope, k, st)
                reparse = reparse or kind == "reparse"
                touched = touched or kind in ("new", "append", "reparse")
            if reparse:
                # 重建集 = 缓存里本 scope 的全部键 + 本次触及的键
                rkeys = {k for k in self._file_cache if k.startswith(prefix)}
                rkeys.update(norm)
                self._rebuild_scope(scope, rkeys)
            if touched or reparse:
                # 失效必须在锁内且在摄取之后:引擎线程下一次调用必走
                # 慢路径重聚合(失效只作用于本实例 —— 生产单实例单引擎;
                # 多实例共享的类级缓存数据本身已是最新,各自 TTL 聚合
                # 视图至多晚一个 TTL 周期自然刷新)
                self._entries = None
                self._scanned_at = 0.0

    def today_usage(self) -> int:
        if not self.is_available():
            return 0
        # date_agg 的键在摄取时按本地天算好(_claude_ts_local 已转本地),
        # 跨午夜只需换查询键,聚合不必重算
        return self._scan().get(dt.date.today().isoformat(), 0)

    def daily_usage(self, days: int = 30) -> list:
        if not self.is_available():
            return []
        # ISO 日期字符串的字典序与日期序一致,直接按串比较裁剪窗口
        lo = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
        return sorted((d, v) for d, v in self._scan().items() if d >= lo)


# discover_sources() 约定:模块级 Source 属性指向本模块的源类(README
# 『如何贡献一个源』)—— 类名本身随意,发现机制只认 Source 这个名字
Source = ClaudeSource
