"""Claude Code 用量源:~/.claude/projects/**/*.jsonl 只读解析。

类与 _claude_ts_local 自 data_engine 原样迁移;v1 起增量偏移解析
(registry#19):文件缓存条目 {mtime_ns,size,offset,entries,date_agg},
追加只 seek(offset) 解析新增字节 —— 单文件成本从
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


def _rebuildable(path: str) -> bool:
    """重建集存在性判据(#60 的判别收窄,P1 2026-10-07):仅
    FileNotFoundError/NotADirectoryError(ENOENT/ENOTDIR,路径确已消亡)
    返回 False —— 已删文件的陈旧条目不得在任何后续 reparse 事件中复活
    重新 claim(#60 意图);其余 OSError(瞬锁/权限/IO 抖动)按『仍在盘』
    保守返回 True。os.path.exists 把两类同判 False,曾把瞬锁的在盘文件
    排除出 reparse 重建 → 共享 mid 归属翻给幸存文件、其 date_agg 未重导
    → 同 mid 双计且无自愈点。放行侧零额外代价:_rebuild_scope 只读缓存
    条目、绝不碰盘,与 _scan 侧重建集并入 errored(『漏并的话重建本身
    就制造一次归属翻转』)同纪律。"""
    try:
        os.stat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True


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
      只是慢到 TTL。

    _file_cache 条目只增不删(#19 改判维持+文档化,评审阻断 2 裁决):
    归来者判定靠 `k in pre_cached`(见 _scan)要求已删除文件的缓存条目
    仍然存在 —— 朴素修剪会让归来文件(回收站还原/同卷 Ctrl+Z)走全新
    冷解析、不再触发归属重建,共享 mid 双计窗口重开(fixed#24 实测修掉的
    今日 666 vs 正确 116 家族)。内存代价每文件条目 KB 级(registry 自记
    量级小);若未来量级恶化,按 tombstone 集合方案另行治理,不在此修剪。

    #92 缓存按 scope 嵌套:_file_cache[scope][file_key](此前扁平
    [file_key] 跨 scope 共享文件级 date_agg,而 mid 归属 _mid_owner 按
    scope 分桶 —— 嵌套 projects_dir 双实例时,第二 scope 对共享文件命中
    mtime+size 短路,直接复用另一 scope 归属下的 date_agg 值,同 mid 跨
    文件双计或子目录清零且不自愈);三缓存(_file_cache/_mid_owner/
    _scope_files)现在同以 scope 为首键,多实例互不串扰的自声明兑现。"""

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
    # 条目五字段(#83 起 seen_mid 影子字段删除 —— 全仓零读取方,归属判定
    # 实际全由类级 _mid_owner 独自承担,镜像冗余只付维护成本):
    # {mtime_ns,size}=上次摄取时的 stat(两者都未变即短路,mtime_ns 比
    # st_mtime 多 100ns 位,原地重写检测靠它);offset=已消费字节数(只
    # 越过完整 \n 行);entries=该文件全部合格行(整文件重解析重建归属的
    # 原料,含被抑制行 —— 重建要按完整行集重导,不能只留归属行);
    # date_agg=本文件归属行的 按本地天 in+out 之和。
    # 首键 = scope(#92:归一化 projects_dir),值 = {file_key: 条目}。
    _file_cache: dict = {}   # scope -> {normpath(file) -> entry}
    # 全局 mid→owner_path 所有权映射(N1 裁决):tail 追加时首 claim
    # 者胜、后来者抑制 —— 与迁移前『每次 walk 一个 seen 集合、先到先得』
    # 同语义,只是把 seen 常驻化以支撑跨轮增量。按 projects_dir 作用域
    # 分桶:去重边界=同目录树跨文件,与迁移前 per-walk seen 完全一致,
    # 测试临时目录/多实例互不串扰。
    _mid_owner: dict = {}    # scope -> {mid: owner_normpath}
    # 每个 scope 上一次 walk 摘到的文件键集:用于删除/改名检测 ——
    # 缓存过的键从 walk 中消失 ⇒ 旧贡献作废,触发一次 sorted 序重建。
    # 不做这步的话,被删文件独占过的 mid 会永远压着幸存文件的同名行,
    # 而迁移前的『每次 walk 重建 seen』在删除后会重新计入 —— 语义必须
    # 对齐。2026-10-03 起兼作『归来者』检测:上轮不在册而缓存有旧条目的
    # 键重新出现 ⇒ 消失轮的重建已动过归属,归来必须再重建一次重导归属
    # (见 _scan 的 returning 注释),否则原样恢复的旧 date_agg 与翻转后
    # 的归属并存双计。(条目本身的只增不删是 #19 维持裁决,见类 docstring;
    # #59 起 note_changes 摄取的新文件也记入本集,两次 walk 之间出生即死
    # 的文件不再逃过 vanished 检测。)
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
        组成字节,行边界在字节态与文本态完全重合。
        #93:except 增补 RecursionError —— 深嵌套毒行(实测最小触发深度
        16916 层,正常 Claude jsonl 嵌套个位数)在 json.loads 内抛
        RecursionError(非 JSONDecodeError 子类),沿 _read_new_rows→
        _ingest→_scan→today_usage 全链无 except 接住,引擎侧唯一护栏
        _poll_stats_tail 只捕 sqlite3.Error → 杀 _db_loop 整卡冻结;
        与 fixed 的 _claude_ts_local 异常面扫尾(fixed 2026-09-30)同族,
        毒行按坏行返回 None 逐行跳过。"""
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
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

    @classmethod
    def _read_new_rows(cls, path: str, offset: int):
        """从 offset 读到 EOF → (合格行列表, 新offset);OSError → None。
        行完整性以 \n 为准:未终止的尾部残行不解析、新 offset 不越过它
        (留在残行起点,下次尾读从残行起点读到 EOF,自然把补全后的整行
        一次计入)。若按残行先计入、补全后再整行重读,会开出『同一行计
        两次』的窗口(v0.2.0 input 双计教训);jsonl 追加以完整行为单位,
        残行只可能是写了一半 —— 本机实测 6 个真实 jsonl 全部 \n 收尾,
        静态缺尾换行(写者崩溃在 JSON 边界)的极端情况接受延迟到补全
        或整文件重解析,绝不吃双计。
        #61:经 cls 分派调 _parse_line(此前硬编码 ClaudeSource._parse_line,
        data_engine 按 isinstance 识别『未来的 ClaudeSource 子类自然继承』,
        硬编码会让子类覆写 _parse_line 被静默忽略,冷/增量两路解析规则
        漂移)—— classmethod 化,子类覆写自然生效。"""
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
            r = cls._parse_line(b.decode("utf-8", "replace"))
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
        『重解析结果 == 从零重建』成为可断言的性质。#92:条目取自本
        scope 桶,不碰其他 scope。"""
        owner = {}
        bucket = cls._file_cache.get(scope, {})
        for k in sorted(keys):
            e = bucket.get(k)
            if e is None:
                continue
            e["date_agg"] = {}
            for ts, i, o, mid in e["entries"]:
                if mid is not None:
                    if mid in owner:
                        continue
                    owner[mid] = k
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
        jsonl 只追加,这是既定事实;真轮转必然伴随收缩或原地重写)。
        #92:条目读写都走本 scope 桶。"""
        bucket = self._file_cache.setdefault(scope, {})
        e = bucket.get(key)
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
                 "entries": [], "date_agg": {}}
            bucket[key] = e
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
            # 作废旧贡献:entries/date_agg 清空换新;归属与 date_agg 交由
            # 调用方紧随的 _rebuild_scope 按确定性序统一重导(见 N1 注释)
            e["entries"], e["date_agg"] = [], {}
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
        (删除在 walk→锁间隙发生时同样被下一轮兜住)。
        #82 目录级瞬盲:os.walk 默认静默吞掉 scandir 错误(AV/OneDrive/
        网络盘瞬锁子目录),部分列举曾被当『真消失』触发空集/部分重建,
        今日聚合清零骤降。现在 onerror 收集:当轮有错 ⇒ 未被列出的已知
        文件按 last-good 保守 —— 并回 present 与聚合键集(与文件级
        errored 同纪律)。vanished 判定跳过一轮(下轮 walk 健康时再裁决:
        真删了照样触发,瞬盲则自愈);returning 刻意不跳(2026-10-09 P1):
        候选必在本轮被 walk 列出,列出即存在,与别处瞬盲无关 —— 盲轮抑制
        returning 却照常提交 _scope_files 会让判定原料被污染而永久失效
        (持久双计),见下方 returning 注释。"""
        now = time.time()
        if self._entries is not None and now - self._scanned_at < self.SCAN_TTL:
            return self._entries
        walked = set()
        walk_errs = []
        for root, _dirs, files in os.walk(self.projects_dir,
                                          onerror=walk_errs.append):
            for fn in files:
                if fn.endswith(".jsonl"):
                    walked.add(_norm_path(os.path.join(root, fn)))
        scope = self._scope()
        agg: dict = {}
        with self._lock:
            # setdefault 取桶(而非 get):摄取循环里 _ingest 对新 scope 会
            # setdefault 建桶,get 返回的分离空 dict 会让下方聚合循环读不到
            # 本轮新建的条目
            bucket = self._file_cache.setdefault(scope, {})
            # 归来者判定的原料:本轮摄取『前』已在本 scope 缓存的键。快照
            # 必须在摄取循环之前取 —— 新文件的条目是循环里 _ingest 才建的,
            # 不先快照就会把『全新文件』误判成『归来者』(见下方 returning
            # 注释)。
            pre_cached = set(bucket)
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
            present = set(keys) | errored
            walk_blind = bool(walk_errs)
            if walk_blind and last is not None:
                # 目录级瞬盲:未被列出的已知文件按 last-good 保守并入
                # present(聚合继续用其 date_agg;判定延后到健康轮)。
                # P1(2026-10-08):『已知』只认 last(_scope_files),不再并
                # set(bucket) —— bucket−last 恰为已被健康轮裁决消失的死文件
                # (条目按 #19 只增不删而存活,携带释放归属前的旧 date_agg),
                # 并回 present 会把死文件旧值原样加回聚合:今日用量虚增其
                # 全额、共享 mid 与幸存文件双计,持续整个瞬盲期。与 #60 在
                # note_changes 立下的『已删文件陈旧条目不得复活』同纪律。
                # last 必要完备:活着/瞬错的文件上轮都在 present(=keys∪
                # errored,含 #59 记入的 note_changes 新文件),盲轮并入后
                # _scope_files 只增不减,无需桶键补漏。
                present |= {k for k in last if k not in present}
            # 消失判定只认 walk(且本轮 walk 无瞬盲):上轮在册而本轮 walk
            # 未列出才是真消失。旧判据 not last.issubset(keys) 把瞬时失败
            # 也当消失:当轮聚合丢 last-good 已违反 _ingest 的 error 契约,
            # 更糟的是用不含它的 keys 重建 → 共享 mid 归属翻给幸存文件;
            # 它恢复后走 unchanged 短路不再重建,同 mid 在两个文件的
            # date_agg 并存 → 今日用量永久双计(input 双计教训家族,
            # 2026-10-02 P1)。
            #
            # 归来者重建(2026-10-03 P1):『上轮在册没有、缓存里却有旧条目、
            # 本轮 walk 又列出』= 消失被观察过、期间已触发把它的 mid 释放给
            # 幸存文件(或清空整个 scope 归属)的重建。『原样恢复』(回收站
            # 还原/同卷 Ctrl+Z move/robocopy 镜像还原/OneDrive 水合,mtime_ns
            # +size 未变)走 _ingest 的 unchanged 短路:条目原样进聚合集,但其
            # date_agg 还是释放前归属下的旧值 —— 与幸存文件翻转后的 date_agg
            # 并存,共享 mid 双计(实测今日 666 vs 正确 116);目录级消失→恢复
            # 形态还会留下 owner 真空,恢复后跨文件同 mid 追加被双 claim(实测
            # 664 vs 114)。归来即重建:按 sorted 序从各文件完整 entries 重导
            # 归属与 date_agg,一次性恢复『owner↔date_agg』一致 —— 重建即冷
            # 扫语义(确定性、幂等),本就是 reparse 事件的自愈通道,普通
            # append 不触发它才让脏状态一直活到下一次 reparse。全新文件不算
            # 归来(条目本轮才建,归属已由 _claim_rows 即时落定,重建是白工);
            # 仅被 note_changes 预摄取过、从未进过 scope_files 的文件会命中同
            # 判据 → 多一次无害重建(重建幂等,结果与冷扫一致),可接受。
            # returning 刻意【不】带 not walk_blind 守卫(P1 2026-10-09):
            # 与 vanished 不同,returning 候选必须在本轮 present(=被 walk
            # 列出的 keys/errored)里 —— 列出即存在,树内别处子目录瞬盲不
            # 影响『上轮不在册、缓存里却有旧条目』这份判定原料。反之,盲轮
            # 抑制 returning 却仍在下方把归来文件提交进 _scope_files 的话,
            # 下一健康轮 `k not in last` 恒假、returning 永不再触发 —— 消失
            # 轮翻给幸存文件的共享 mid 与归来文件未重导的旧 date_agg 持久
            # 并存,今日/按天用量持久双计(2026-10-03 已修 P1 在 #82/#122
            # 盲轮路径上的复活形态;探针实测 unchanged/errored/append 三条
            # 进入路径全中,跨多轮健康轮不愈)。盲轮即刻重建亦安全:present
            # 已并入 last-good 保守集(:405),重建集完备,且重建幂等。
            returning = (last is not None
                         and any(k not in last and k in pre_cached
                                 for k in present))
            if reparse or returning \
                    or (last is not None and not walk_blind
                        and not last.issubset(walked)):
                # 重建集并入瞬时失败文件(其条目未动,按完整行集重导
                # 归属)——漏并的话重建本身就制造一次归属翻转
                self._rebuild_scope(scope, present)
            self._scope_files[scope] = present
            for k in present:
                e = bucket.get(k)
                if e is not None:       # 冷失败无条目自然跳过(旧 errored 分支)
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
        波及全局归属,按 sorted(path) 重建整个 scope(N1)。
        #59:摄取到的新文件(kind=="new")同步记入 _scope_files ——
        整个生命周期落在两次 walk 之间的文件(经 watcher 摄取、又在下次
        walk 前删除)此前永远逃过 vanished 检测,其独占 claim 的共享 mid
        永久压制幸存文件同名行;记入后下次 walk 不列出它即触发重建解禁。
        #60:重建集 rkeys 过滤已消亡路径 —— 陈旧条目在任何后续 reparse
        事件中不再复活重新 claim。判据用 _rebuildable(仅 ENOENT/ENOTDIR
        算消亡)而非 os.path.exists(对『瞬锁不可读』与『已删除』同判
        False):瞬锁的在盘文件被排除出重建会让共享 mid 归属翻给幸存文件
        → 双计,且此后 unchanged/append 均不触发重建、无自愈点(旧注释
        称『由下次全量重建自愈』,该触发并无周期性保证,已证伪)。"""
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
            bucket = self._file_cache.setdefault(scope, {})   # _scan 同款取桶
            touched = reparse = False
            new_keys = set()
            for k in sorted(norm):       # 确定性序:同批多路径的去重顺序可复现
                if not k.startswith(prefix):
                    continue
                try:
                    st = os.stat(k)
                except OSError:
                    touched = True        # 删除/改名:失效 TTL,walk 侧裁决
                    continue
                kind = self._ingest(scope, k, st)
                if kind == "new":
                    new_keys.add(k)       # #59:出生文件记入 scope 在册集
                reparse = reparse or kind == "reparse"
                touched = touched or kind in ("new", "append", "reparse")
            if new_keys:
                self._scope_files.setdefault(scope, set()).update(new_keys)
            if reparse:
                # 重建集 = 缓存里本 scope 仍存在的键 + 本次触及的键(#60;
                # 『仍存在』按 _rebuildable 判:确已消亡才排除,瞬锁在盘
                # 必须并入 —— 重建只读缓存条目,漏并即制造归属翻转双计)
                rkeys = {k for k in bucket if _rebuildable(k)}
                rkeys.update(k for k in norm if _rebuildable(k))
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
