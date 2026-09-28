# Changelog

## v0.8.0 (未发布)

- **玻璃仪表视觉改版**(spec=`design/design-preview-abd.html` 归档件,方案
  A+B+D 融合的**不透明近似版**:D 的玻璃质感底 + A 的仪表层次(脉冲环/大数字/
  氛围光晕)+ B 的图形化(sparkline/环形进度),accent 绿→系统蓝亮变体、警示
  琥珀。DWM/亚克力/真窗口透明按 spec 风险区**不做**,setWindowOpacity 既有
  frozen 0.8 冻结机制原样保留;贴边/吸附/保护期几何与全部数据口径零改动。
  五票分批合入:基建(T1)/数据层(T4)→ 卡片(T5+T2)→ 条(T3))
- **色板/字体基建(T1)**:旧名改值、引用零改动(四常量名在 app.py 引用
  54 处/41 行,本会话 grep 词边界实测;重命名大 diff 高风险):`C_ACCENT`
  `#5ad6a0→#7ad7ff`、`C_WARN #e8c268→#ffd166`;`C_FG/C_DIM` 与 QSS faint
  字面改**白 alpha 叠 #17181d 的逐通道实算合成 hex**(白.9→`#e8e8e8`/
  白.55→`#979799`/白.35→`#68696c`)。**F2 硬约束**:`C_FG/C_DIM/C_BG/
  C_BORDER/C_ACCENT/C_WARN` 连同新增 `C_ACCENT_DIM=#60cdff`、`C_TIER_OK=
  #6ee7a8`、`C_TIER_DANGER=#ff6b6b` 必须保持 QColor 可解析 hex —— BarChart
  直接 `QColor(C_FG/C_DIM)` 消费且历史窗零测试覆盖,rgba 字面实测
  `isValid()==False` 会静默画黑;半透明白只允许出现在 QSS 字面与 painter 的
  `QColor(255,255,255,a)` 构造。**补充白梯度档(N2)**:主三档外补 soft=204
  (卡片 grid 六格 v)/half=128(模型行速度)/vstrong=217(竖条组 v),仅以
  QSS objectName selector(`QLabel#soft/#half/#vstrong`)rgba 字面落地,
  不新增 C_* 常量(hex 约束不破)。字体:等宽主族 Consolas→**Cascadia Code**,
  新 `mk_mono(size, weight)`(`setFamilies(['Cascadia Code','Consolas'])`,
  未装 Cascadia 的机器回退 Consolas 数字宽度不漂;中文/⏱ 不列进 families、
  走 Qt 字体合并,v0.7 ⏱ 先例),`_mk_lbl` 内部单点接线+直接 QFont 调用点
  逐处换。**tier_color(pct) 纯函数**(同 frozen_opacity 单测先例):
  `>50→C_TIER_OK;20≤pct≤50→C_WARN;<20→C_TIER_DANGER`,边界 50.0/20.0 归
  WARN、19.9 归 DANGER;**与用户 alert_pct 刻意不动态绑定** —— 阈值可配多级
  (如 30/10),分档色是视觉语言非告警状态,动态绑定会让该配置退化成两档;
  20 与默认告警首阈值 `[20,10]` 巧合对齐纯属巧合。test_stress 补 F2 hex
  可解析守卫与三档/边界注入断言
- **速度趋势数据层(T4)**:`DataEngine.fetch_recent_speeds(n=12,
  session_id=None)` —— `status='completed' AND session_id=? AND
  query_source='main_turn'`(与 _poll_stats 主速度查询同型,v0.2.0 subagent
  劫持会话统计事故红线:混入即曲线与主速度口径不符)再叠加
  `duration_ms IS NOT NULL AND output_tokens>0`(分母/分子无意义的行不进
  曲线);`ORDER BY rowid DESC LIMIT n` 后 Python 侧反转成**时间正序**;
  `speed = output/(max(duration−COALESCE(ttft,0),1)/1000)`(max(,1) 防零除,
  与 _poll_new_completed 同式)。sqlite3.Error→返回 `[]`(与 _poll_stats
  同吞法,坏库不炸引擎线程,UI 按 <2 点隐藏不闪空),con.close 置 finally;
  `Snapshot.recent_speeds` 由引擎线程在 snap_lock 内 1s 一查填充(与
  today_by_source 同款,_switch_session 重建后下一拍补全)。test_data_engine
  按合成库约定新增断言:多行值手算对账/时间正序/n 截断/subagent 与
  cancelled 排除/异常路径空表;README 口径表新增『速度趋势(sparkline)』行
- **可复用绘制件+卡片重绘(T5+T2,依赖 T1)**:模块级 `paint_sparkline`
  (#60cdff 1.5px 圆帽折线+末端点 #7ad7ff r=2;x 均分、y min-max 线性映射
  上下各 1px inset;min==max 画水平中线;n<2 return)、`paint_ring`(底环
  白.10 3.5px、前景 3.5px 圆帽、12 点起顺时针 pct%·360°,pct 钳 [0,100],
  中心文字由调用方叠加)。三 QWidget(sizeHint 固定):**PulseIndicator**
  (替换旧 QLabel 呼吸点:active=蓝环+内核+外扩圈 2s ease-in-out,相位复用
  _breath/60ms timer 折算不新建定时器,仅 generating 时 update(),idle=白.35
  静态空心环;卡 14/横条 10/竖条 12)、**SparklineWidget**(卡 72x24/横条
  44x16/竖条 60x14)、**RingWidget**(卡片 46px 中心 N% 11pt tier 色 Bold/
  横条 12px 无字)。M5 中间态分派 `_set_dot_active`(isinstance
  PulseIndicator→set_active,else 旧 QLabel 分支)随 T3 合入删除。**窗口背景
  改自绘**(唯一样式机制切换点):QSS `#root` 删 background/border(QSS_BAR
  的 replace 派生链保留防静默断链,无 background 时 border-radius 不绘制
  任何东西),`MeterWindow.paintEvent` 画垂直渐变 `#17181d→#131419`+顶部
  中央 200x100 径向光晕 rgba(96,205,255,.10)→透明(裁进圆角)+1px 白.08
  描边;**圆角归属钉死在形态**:卡/竖条 12、横条 8。卡片新结构(预览
  .g-card):①状态行=Pulse14+『生成中 Xs』(elapsed 并入,idle『空闲』)+
  右侧模型名(📌 前缀、超宽 elide)——**会话标题不再占行,完整标题+📌 进
  窗口 setToolTip**;②主数字行=速度 30pt Bold 蓝+『tok/s』11pt 白.35+
  sparkline;③今日 hero 三段:fmt_k 19pt Bold 白.9+金额 13pt 白.55 600+
  『今日』10pt 白.35(cost=0 省金额段、partial `≈` 逐字保留);④套餐
  section **容器化**:分节线+Ring46+右信息三行包进同一 QWidget,`_plan_pct`
  缺席整组 `setVisible(False)`、分节线不悬空;⑤grid 六格 3 列(入/出
  `v=f"{fmt_k(in)} / {fmt_k(out)}"` **直取 session_in/out 禁碰 session_cache**
  (v0.2.0 input 双计 cache 回归红线)、缓存命中、⏱首/总、燃速、均燃、均速),
  k 9pt faint/v 13pt soft,固定列宽 `CARD_GRID_COL_W=(101,81,111)`(满载
  文本自然宽实测 372>313,elide+tooltip 回读,合计恰收敛 313);⑥模型列表
  rows[:4] 容器化(名左 faint/速度右 half,替换旧单 QLabel join)。**尺寸
  重估**(禁拍脑布,v0.5.1『336 截 342』教训):`CARD_W=313`(设计宽,
  `__init__` adjustSize 后 clamp,防启动路径与常量脱节)、`CARD_H=346` 沿用
  —— `findings/measure_card_baseline.py` 三道闸(原生平台/processEvents 后
  adjustSize/满载<300 判 INVALID)实测满载(plan/burn on+sparkline 12 点+
  多源)0-4 行模型 276/293/310/327/344,max=344、+2 余量;0/1 行不再同高
  (plan 容器恒占)
- **横竖条重绘(T3,依赖 T1+T5+T2)**:横条新段序:脉冲 10|速度 14pt Bold
  蓝+『t/s』|sparkline 44x16|sep_plan|套餐段=Ring12(**实画小环,不用 ⊙
  字形** —— Cascadia 无字形保证,⏱ 先例)+『N% ~X』tier 色 600+倒计时
  dim|sep_today|今日段(`今{fmt_k}`+` ≈¥{cost:.0f}` 金额取整;**cost=0
  整个金额段省略**、partial `≈` 前缀保留,与卡片同守卫)|sep_burn|燃速段
  (瞬时优先口径不动)。**旧横条的 avg/ttft/dur/in/out/rate/elapsed 段全删**
  (不做迁移补偿 UI,除已裁决的 tooltip 两处)。竖条(全 AlignHCenter):
  脉冲 12|速度 24pt Bold 蓝|『TOK/S』9pt|sparkline 60x14|今日组|套餐组
  (v=N% tier 色/k=『~X · cd』,lt/cd 缺段自然省略、空列表置空但组结构保留)
  |燃速组(v=`296M/h` 式/k=『燃速 · 均137』式含均燃)|入出组(**v=入量、
  k=『入 · 出X』 —— 出量并入 k 行文本,竖条缓存率随之移除**)。**竖条定宽
  116(F1,全 spec 唯一明示的尺寸机制变更)**:新类常量 `BAR_V_W=116`,
  `_bar_size` 竖向分支宽度侧由 max_w 聚合+`min(...,100)` cap 改定宽常量
  (高度侧聚合与跳过 hidden 不变;横向分支一字不动;_apply_dock_geometry/
  _settle 零改动)—— 与旧 cap 同款哲学,文案已钉死紧凑形,超宽硬截属预期。
  vsep 竖向线宽 92→84 重锚(BAR_V_W 116−左右边距 8−边框),竖条水平边距
  钉 8。**分隔线显隐(F4)**:横条 sep_plan 跟套餐段/sep_burn 跟燃速段/
  sep_today 恒显(今日恒在;旧 _budget_sep 机制升格为逐段 sep 属性);竖条
  vsep_plan 跟套餐组/vsep_burn 跟燃速组/vsep_today/vsep_in 恒显 —— 数据
  缺席时段与其前分隔线一并隐藏,无悬空线。**None/属性总表(第 2 轮评审
  修正,卡|横|竖三列逐属性,唯一权威)**:三形态恒建=dot/tps/today/burn/
  plan 五 label+spark;卡建条 None=state/model_rows/today_src/timing/
  in_out/avg_burn/rate/avg+plan_cap/plan_tok(细分件 model_name/today_cost/
  tps_unit/plan_section 容器同列);卡+竖建=plan_sub(横条套餐段
  自带文案不建);plan_ring=卡 46/横 12、竖 None;plan_cd=横建(cd 缺→置空
  并隐藏,条形态隐藏非空文本纪律);in_lbl/vbar_in_cap/vbar_burn_cap=竖建;
  **out_lbl 三
  形态全 None**(出量并入 vbar_in_cap 文本);sep_*=横建、vsep_*=竖建、卡片
  三主分节线恒建;**title/est/elapsed/cache/ttft/dur 三形态全 None**(ttft/
  dur 已并 timing,卡片也 None)—— 实现与 stress 属性×形态矩阵断言同源,
  漏置 None 会让形态循环摸已销毁对象(v0.4.0 教训)
- **可见信息取舍(补偿已裁决)**:est_hours_left『还可撑 X 小时』从横条
  段+卡片行**降为卡片燃速格 tooltip**(『预算还可撑 X.Xh/预算已超支』,
  预算告警气泡保留作主动提醒 —— 直读习惯被打破的补偿);会话标题行取消,
  完整标题+📌 进窗口悬停提示。两处均为已发布文档化行为的有意变更,README
  五处同步(特性行/交互表/图例行/口径表『还可撑』行+sparkline 新行)
- **stress 断言整批同步**:**『条形态文案一字不动』红线(本文件 v0.7 条目
  记载)此次作废**,断言批量重写只发生在 T3/T5+T2 步:①属性×形态全组合
  矩阵(按 None/属性总表);②尺寸断言:竖条宽 `≤90`→`==BAR_V_W(116)`、
  横条高 `≤30`→`≤34`(14pt+16px sparkline 实测上限,注释写明);③套餐/
  倒计时/timing(『0.8 / 12.4s』式)/plan 钉死表/today 三段式+cost=0 省金额
  段新文案;④tier 三档注入(87/40/15→RingWidget 与文本色==tier_color);
  ⑤sparkline <2 点三形态隐藏;⑥分隔线显隐(数据缺席时段与其前 sep/vsep
  一并隐藏);frozen_opacity 断言一字不动
- 归档:spec 预览原件 `design-preview-abd.html` 普通移动入 `design/`
  (git status 实测 ?? 未跟踪,无历史可保;design-preview.html /
  design-preview-cd.html 不动)
- 人工目视清单(回归与 offscreen 截图均不能替代,发版前过一遍):①三形态
  深渐变底/圆角 12-8-12、**无白底闪现**(QSS background 移除+paintEvent
  自绘是唯一样式机制切换点,ui-verify 无自动检测手段);②顶部光晕可辨
  (rgba 蓝 .10,弱但应在);③三档色环注入 87/40/15 对照(绿/琥珀/红,
  ring 与文本同步);④脉冲环三尺寸(14/10/12)动画与 idle 静态空心环;
  ⑤sparkline 末端点(蓝点 r=2)在场;⑥125%(及如有 100/150%)DPI 下 hero
  三段不横向截断(v0.7 清单⑧先例)

## v0.7.0 (未发布)

- **卡片信息层级重排**(提案#3):卡片重排为 tok/s 20pt 主数字(不动)→
  **今日用量+金额升为第二主数字**(『今日 X · ≈¥Y』,C_MONO 13pt Bold 白色;
  cost=0 省略金额段、partial `≈` 下限口径逐字保留)→ 8pt 多源行(>1 源才显示,
  单源置空)→ 主分节 → 统计 3×2 grid → **套餐剩余两级化**(主数字『套餐剩余
  N%』13pt Bold warn + 8pt faint 副文本『· N分钟前 · Xh Ym 后重置』,`_plan_pct`
  缺席时两级整体省略)→ 燃速行(文案不动,移序)→ 次分节 → 模型列表。
  **ttft/dur 两格合并为单格**『⏱ 首字 Xs · 总 Ys』进统计 grid 右列(缺参
  占位 `--`,整行结构恒保留;⏱ 走 Consolas→Segoe UI Symbol 字体回退)。
  次分节符自诞生即**内联样式**(零 QSS/objectName 依赖,跨 ticket 合入无
  中间态空行);条形态(横/竖)文案与结构一字不动(stress 精确断言红线)。
  布局重建 None 纪律:卡片不再建的 ttft/dur 与条形态不建的今日 hero/多源/
  套餐副文本三 label 均显式置 None(v0.4.0『压力循环摸已销毁 QLabel』教训)。
  `CARD_H` 按重排后满载自然高度**原生实测重估**(旧 350 已容不下重排后的
  3-4 行模型;规则=满载矩阵 0-4 行全不截断,实测超限则退守『≥2 行满载
  不劣化』,截断点沿 v0.5.1 格式写进类注释),`_restore_state`/
  `_detach_to_pointer`/`_unset_dock` 引用常量自动跟进(禁字面量)。测量工具
  `findings/measure_card_baseline.py` 固化三道闸:注入后 `processEvents()` 再
  `adjustSize()`(缺这步读未激活垃圾塌成 200x22)、显式禁 offscreen 平台、
  满载 max_h<300 打 INVALID 退出码 1。test_stress 追加卡片结构断言
  (新三 label 在场/ttft+dur 为 None)与钉死期望串全组合断言(⏱ 四组合、
  套餐 hero/sub 六组合),现有断言一字未改
- **间距标度+分节符分级**(提案#6,须在前条合入后执行——同改 `_build_card`/
  CARD_H):模块级间距常量 `SP={"xs":4,"s":6,"m":8,"l":12,"xl":16}`(8pt 栅格
  半步档;registry 提案原文『四档』以本需求五档为准),四个构建器
  (card/bar/settings/history)的布局魔法数按**值保真映射**就近替换:
  card 边距 (14,12,14,10)→(16,12,16,12) 即 +4宽+2高、Settings 顶边距 14→12
  即 -2高、History root (12,10,12,10)→(16,12,16,12) 即 +4高(窗口尺寸固定
  无感)、其余 margins/spacing 数值全部不变。`_bar_size` 的 sp 与 16/2+2
  边距算术改为 SP 同源引用+注释锚定(值保真 ⇒ 横条高≤30/竖条宽≤90 与
  尺寸稳定断言零漂移,漂移即视为实现错误回退);豁免清单(label-value 配对
  行距 2、QSS 内 padding、BarChart painter 常量、对话框最小宽高)就地注释、
  不动值。次分节符内联色换新常量 `C_BORDER_SUB`(#232631,介于 C_BG #16171c
  与 C_BORDER #2c2f3a 之间)实现主/次分级——这是 T-B 唯一样式动作;主分节符
  QSS `#sep` 机制、其余 C_* 与深色主题不动。映射后复跑满载测量矩阵,若
  H_max 变化需同步 CARD_H 与类注释
- 人工目视清单(回归与 offscreen 截图均不能替代,发版前过一遍):①卡片层级
  递降 tok/s 20pt→今日 hero 13pt Bold(**应为 C_FG 白,不是默认 dim 灰**)→
  普通字段 9pt;②套餐剩余主数字醒目、『· N分钟前 · Xh后重置』副文本明显弱于
  正文(faint);③⏱ 合并行可读、字形正常渲染;④满载卡(3-4 行模型+多源+
  quota+预算)底部模型列表不被截断(ui-verify 只打印尺寸不校验截断);
  ⑤主/次分节符肉眼可分(#2c2f3a vs #232631);⑥横条/竖条与改前逐像素无
  差异;⑦设置窗(-2 高)/历史窗边距收紧后无控件挤压换行;⑧125%(及如有
  100/150%)DPI 下 hero 行不横向截断(最宽 13pt 文案 194px < 内容区 218px)

## v0.6.0 (2026-09-27) — 项目结构标准化:src 布局迁移(仅移动与引用修复,零行为变更)

- **src 布局**:三个核心文件移入 `src/zcode_meter/` 包(git mv 保历史)——
  `data_engine.py` 原位更名不变;`zcode_meter_qt.py` → **`app.py`**(UI 入口);
  旧 tkinter 版 `zcode_meter.py` → **`legacy_tk.py`**(仅留档,自包含零 import
  改动,其调试日志落包目录属预期);新增 `__init__.py`(仅 docstring +
  `__version__ = "0.6.0"`,**零副作用**:不 import 包内模块 —— data_engine 一被
  导入就启用 faulthandler 写 zm_crash.log,`import zcode_meter` 必须保持可无开销
  用于版本探测)。根目录 `__pycache__/` 残留清除
- **向后兼容 shim**:根目录保留 3 行 `zcode_meter.py`,把 `src/` 插到
  sys.path[0](必须先于脚本目录,否则 `import zcode_meter` 解析到 shim 自身即崩)
  后转发 `zcode_meter.app.main()`;v0.5.x 旧命令(`pythonw zcode_meter.py`)继续
  可用,README 已标注新入口为 `pythonw src/zcode_meter/app.py`
- **app_dir() 路径不变式**(本次唯一数据层逻辑改动,目的恰是保持行为不变):
  脚本分支从包目录向上找含 `README.md` 的祖先目录=仓库根(找不到回退包目录),
  frozen 分支逐字未动。否则六个用户文件(`zm_config.json`/`zm_prices.json`/
  `zm_alerts.json`/`zm_state.json`/`zm_debug.log`/`zm_crash.log`)会随文件移动
  无声搬到 `src/zcode_meter/` —— quota key 失配(load_config 吞 OSError 静默回退
  默认)、位置记忆清零、燃速/告警口径数据断档,全部无报错,属最隐蔽回归
- **import 修复**(全部改为包路径,禁止 `src/zcode_meter` 与根目录双路径共存 ——
  那会让 data_engine 加载成两个独立模块实例,QuotaMonitor/缓存身份分裂):
  app.py 头部加脚本引导(直跑时补 src 进 sys.path 后同一 continue 执行,刻意
  不 re-import 自身,防 __main__ 副本与包模块两套类对象);tests/test_data_engine.py
  12 处、test_stress.py 1 处、run_all.py ui 组命令改 `src/zcode_meter/app.py`。
  **现有全部测试断言一字未改,只改 import/路径行**(tests/debug/ 经核实零处引用,
  不动)
- **测试**:新增 `tests/test_package.py` 五项守卫(包可导入/版本号/`zcode_meter.app`
  可导入/`app_dir()==仓库根` 钉死落点不变式/shim `--verify` 链路),run_all.py
  新增 pkg 组并纳入默认组列表(漏加则「全绿」静默漏跑)
- **文档**:README「快速开始」/自检/开机自启注册表命令/打包命令(改
  `pyinstaller --onefile --noconsole --name zcode-meter --paths src
  --exclude-module tkinter src/zcode_meter/app.py`,`--paths src` 缺了会打包成功
  但 exe 启动即 ImportError;`--exclude-module tkinter` 红线保留)/架构树/开发指南
  全部同步;「配置」章节注明 zm_* 文件落点(开发模式=仓库根,frozen=exe 目录);
  .gitignore 补锚定说明(现有 basename 模式移动后仍全部命中,无需改规则)
- 口径红线:除 app_dir 路径解析外不碰任何 SQL/统计/渲染逻辑,「显示指标与口径」
  表全部不受影响;以迁移前后 run_all 全组输出逐组对照验收

## v0.5.1 (2026-09-27) — quota 事件驱动+节流 / 倒计时本地化 / 数据新鲜度标注

- 调度改造(`data_engine.QuotaMonitor`,仅内部调度,对外接口不变):300s 盲轮询
  改为**事件驱动+节流** —— `DataEngine` 新增 completed 行水位监测
  (`_max_completed_rowid`/`_check_activity`,每秒查一次水位,单次实测 ~0.004ms),
  水位前进 = ZCode 有新请求完成 = 『有消耗』,经新回调 `on_activity` 转发到
  `QuotaMonitor.notify_activity` 记录活跃时刻;monitor 改 1s tick 检查纯函数
  `quota_fetch_decision(now, last_fetch, last_activity, force)` 决定是否真发请求:
  **最短间隔 60s 硬闸**(常规路径被 180s 常态闸覆盖、不可达,作防御性下限与未来
  触发类型保留)/ **活跃期(过去 1h 内有请求)常态最长 3min 一查** /
  **静默期(>1h 无任何请求)完全暂停** / **启动与设置窗换 key 后立即查一次**
  (首轮 force,与旧 run() 先查后等行为一致)。全会话全 query_source 计入
  (含 subagent,与今日用量口径同宽,刻意设计);cancelled 不算。失败也推进
  `_last_fetch_ts`(失败占频率预算,防 1s tick 对故障端点加密重试);成功把
  `fetched_at` 附加进 latest() 结果(parse_quota_payload 契约不变)。频率对账:
  旧版恒 288 次/天,新版静默 0 次、活跃上限 20 次/h,典型日(4h 活跃+20h 静默)
  ≈80 次/天,整体显著下降
- 倒计时本地化(对标 GLM Monitor 已验证做法):拿到 `next_reset_ms` 后重置
  倒计时纯本地计算,不再为倒计时发任何 API 请求 —— 新纯函数
  `format_countdown_hm`('Xh Ym'/'Ym',分钟向上取整,过期/缺参 → None)在
  渲染 tick 每 200ms 重算(分钟粒度=每 60s 递减);卡片套餐剩余改两行文案:
  第一行「套餐剩余 N%」+ 新鲜度(`format_age_zh`:刚刚/N分钟前/N小时前),
  第二行「Xh Ym 后重置」(缺数据自然省略)。**条形态文案一字不动**(test_stress
  精确断言红线),只有剩余% 仍需网络刷新
- 数据新鲜度透明化:套餐剩余旁标注查询时间(「· 3分钟前」),静默期数据冻结
  从不可见变为可见 —— 需求点 2/4 的明确取舍:不再每 5min 自动刷新,搁置一夜
  后首看是陈旧百分比+「N小时前」,首次活动后最迟 3min 刷新(README 口径表已写明)
- 设置热生效(`_apply_config`):启用/换号分支在 monitor 重启后**重挂
  `eng.on_activity` 到新实例**(漏挂则换号后只剩启动首查、quota 永不刷新);
  清号分支摘除回调并随三缓存一并清 `_plan_fetched_at`/`_plan_next_reset`
  (扩展 v0.5.0『旧账号套餐数据零残留』不变式)
- 尺寸:`CARD_H` 336→350(卡片套餐剩余第二行使 2 行模型+多源自然高度 342>336
  被钉死几何截断;350 可容 2 行、3 行起 356>350 仍截断,与旧 336 截断点同点
  无回退;`_restore_state`/`_detach_to_pointer`/`_unset_dock` 走常量自动跟进,
  ui-verify 只打印尺寸不校验截断,需人工目视)
- 兼容(全部不动):`parse_quota_payload`/`snap.plan_remaining_pct`/设置界面
  契约不变;`ZM_NO_STATE=1` 与 `--verify` 下 monitor 不启动不写盘、未接线引擎
  的 `_check_activity` 对 None 短路(一切现有测试路径行为与旧版逐位一致);
  接口失败静默降级(`_fetch_once`/`_log_parse_fail`)保留
- 测试:`test_data_engine.py` 新增四组单测(全部无网络、不 start 线程、不写盘)
  —— 节流决策四场景+整数边界(59.9/60/179.9/180/3600 整、force、双 None)、
  倒计时与新鲜度文案(含边界与缺参)、活动信号恰触发一次/未接线短路不查水位/
  坏回调兜住、monitor 记账(fetched_at 附加/失败也推进 `_last_fetch_ts`);
  README 口径表与配置节的查询频率表述同步改事件驱动口径

## v0.5.0 (2026-09-27) — 设置窗 / 图表竖柱统一 / 贴边条预算段

- 设置窗：主窗右键新增「设置」（原菜单项一字未动，新项插在「历史用量图表」
  之后），深色 QDialog（QSS_SET 复用 QSS_HIST 色板）编辑
  `quota_api_key`/`daily_budget_cny`/`alert_pct` 三字段；key 用
  `EchoMode.Password` 密码框（预填现值但任何回显均为掩码，日志/报错路径
  不拼 key 明文）；对话框延迟到菜单模态循环返回后 `QTimer.singleShot(0)`
  再 exec —— triggered 槽内直接弹窗会被菜单关闭的鼠标抓取吞掉（v0.3.0
  历史窗口同款坑）。非法输入或保存失败只红字报错、不落盘不关窗；阈值
  分隔符兼容半角/全角逗号与空白（中文输入法高频），数值判定以
  `float(strip())` 为准（QDoubleValidator 仅做输入反馈）
- 落盘语义（`data_engine.save_config`）：与 `load_config` 完全同规则规范化
  后**临时文件 + `os.replace` 原子写** —— 直接 open('w') 中途崩溃会坏
  `zm_config.json`，而 load_config 对坏文件静默回退默认，key 会无声丢失；
  OSError（exe 只读目录等）清残留临时文件后返回 False 不抛错；默认路径
  在 `_no_persist()` 守卫（ZM_NO_STATE=1 / --verify）下返回 False 且不写
  —— 环境残留时设置窗仍可打开，若返回 True 会让 UI 热生效并关窗而 key
  从未落盘、重启即无声丢失（『用户以为改了实际没改』红线）；显式 path
  参数供单测绕过守卫
- 保存热生效（`MeterWindow._apply_config`）：先 save_config，失败即整体
  终止（内存与磁盘不脱节）；成功后同步 `self.daily_budget_cny` 与
  `eng.daily_budget_cny`（裸写先例=quota_hint，GIL 原子，1s 内重算
  est_hours_left）→ 就地更新 `alerts.thresholds`（不重建 BudgetAlerts，
  zm_alerts.json 已触发状态保留）→ 与 `__init__` 存的 `_quota_key` 内存
  基准（**严禁落盘后回读文件比较**）四分支对账：不动/启动/停+清
  _plan_pct·snap.plan_remaining_pct·eng.quota_hint 三缓存/换号停+清三缓存
  +立即按新 key 重启（清号/换号后旧账号套餐数据零残留，monitor 不停在
  None）。注记：同日新增/调低阈值后新级别可能当日补发一次气泡（同级别
  同日仍只提醒一次，属预期）
- 历史图表统一竖柱：删除 `BarChart` 的 `horizontal` 参数与 `_paint_h`
  （水平条形态不可再得 —— 三图全竖柱是需求方明确指定），新增
  `label_angle`；会话/计费块页 45° 斜排长标签（以柱中心为锚、右端落在
  锚点、按可用对角线长度 elide、首尾标签受边缘钳制、bot 边距按旋转投影
  自适应并设上限），相邻标签为平行带只需法向间距≥行高 → 会话页 20 项
  全画不抽稀；抽稀步长由硬编码 '09-26' 宽度改为实际最长标签宽度投影
  （按天页 0° 外观与抽稀密度不变）；竖排数值/第二行 ¥/当前块 C_WARN
  高亮与三条取数 SQL 一字不动。**取舍明示**：45°+elide 后会话页标签仅
  剩约 6~12 个字符（对比水平条约 20 字符），信息量损失以悬停 tooltip
  （全量 label+数值，`setMouseTracking`+`mouseMoveEvent`）补偿
- 贴边胶囊条预算段：横条在今日组后插入『套餐剩余 N%』（warn 色）与
  『燃速 x/h』（dim 色）带分隔线；竖条空间受限只加紧凑『套 N%』
  （≈30px，守住 stress 的竖条宽≤90 断言）。数据缺席整段
  `setVisible(False)`（label+分隔线），`_bar_size` 跳过 isHidden 控件
  —— 否则隐藏 label 仍按 sizeHint 计入会让条宽虚胖、空文本 QLabel 占行
  高会撑破『横条高≤30』断言；卡片形态维持纯文本切换不隐藏（现状）。
  `_build_card`/`_build_bar` 设 `_bar_form`=None/'h'/'v' 与实际标签集合
  一一对应，尾部置 None 纪律只保留真正不创建的 label（burn/plan 改由
  各形态自行创建/置 None，防压力循环摸已销毁 QLabel 的 v0.4.0 同类 bug）
- 测试：新增 save_config 两组单测（显式临时 path 写读回环逐字段相等 +
  坏形规范化为默认；守卫命中零文件、temp 创建失败无残留、Windows 打开
  中目标触发 replace 失败后原文件不变且 .tmp 已清理）；test_stress 追加
  预算段结构（横条 plan+burn/竖条仅 plan+burn 为 None+_bar_form 对应）
  与注入数据后的双形态显隐·尺寸稳定·高度≤30·宽度≤90、数据缺席整段
  隐藏断言；现有测试与断言一字未改
- README：交互表加「右键 → 设置」与图表悬停说明；配置章节『改动需重启』
  改为『设置界面保存即生效（直接改文件仍需重启）』，注记阈值当日补发
  气泡与保存失败报错语义

## v0.4.0 (2026-09-26) — 成本估算 / 预算告警 / 燃速 / 计费块 / 多源 / 托盘

- 成本估算：内置 bigmodel 按量刊例价三档表（元/M tokens，2026-09 自官方定价文档
  转录；`GLM-5.3-Flash` 取标准牌价、分档计费模型取最高档保守估算），`zm_prices.json`
  按模型级 merge 覆盖（坏文件回退默认）。口径钉死两条：已知模型缺 `in_cache` 档
  → cache_read 按 in 全价计（宁可高估、不置 partial）；未知/非 bigmodel 模型 →
  ¥0 + partial（卡片/横条金额带 ≈，为下限）。卡片今日用量、横条、按天图表（柱身
  第二行）同步显示 ¥。今日金额/燃速口径均为 **ZCode-DB-only**（Claude 源不进金额）
- 预算告警（双轨）：quota 轨 —— GET `open.bigmodel.cn/api/monitor/usage/quota/limit`
  （只读，全程唯一外部请求端点，Authorization 放原始 key 不带 Bearer，300s 一轮，
  urllib 标准库实现），`percentage` 为已用%、取 TOKENS_LIMIT 中 `number==5` 的 5h 窗，
  低于阈值托盘气泡提醒；按量轨 —— (日预算−今日花费 ZCode 口径)/日预算。阈值默认
  剩余 20%/10% 各提醒一次，同级别同日只提醒一次、跨日自动重置（独立 `zm_alerts.json`
  持久化，守卫环境不写）；接口结构变化时该轨优雅降级为不显示，首跑失败把响应片段
  落 `zm_debug.log`（社区逆向接口，无 key 无法预联调，需真实 key 人工验证一次）
- 燃速 + 耗尽预估：trailing 60 分钟窗口计算 token/¥ 每小时燃速，`(日预算−今日
  花费)/燃速¥/h` 显示"预算还可撑 X 小时"；燃速 0 或未配日预算返回 None；活跃不足
  60 分钟时窗口未满会低估燃速（README 口径表注记）
- 5h 计费块：历史图表第三页签，按 5 小时窗聚合 query_source 全部 completed in+out
  （同今日 token 口径），当前活动块 C_WARN 高亮；quota 可用时以
  `nextResetTime−k×5h` 对齐平台真实计费窗，否则回退锚点=最早 completed started_at
  （块界为示意，页签内声明）
- 多 CLI 聚合：数据层抽象 `UsageSource` 接口（name/today_usage/daily_usage），
  `ZCodeSource` 包装现有口径为默认实现；新增 `ClaudeSource`（`~/.claude/projects`
  只读 jsonl 解析：in=input+cache_read+cache_creation 补齐 Anthropic 口径、含
  sidechain、message.id 去重但先跳过零用量/error 行防清零、UTC→local 定天界、
  (path,mtime,size) 文件缓存 + 15s 扫描 TTL）；卡片今日用量下方聚合显示各源。
  **今日用量本体 `today_tokens` 及其 SQL 保持 ZCode-DB-only 一字未动**（历史回归
  重灾区），多源只落新字段 `today_by_source`
- 托盘模式：`QSystemTrayIcon`（无托盘环境整体跳过不崩）；主窗右键「收起到托盘」，
  托盘菜单 显示/隐藏+退出，单击恢复；预算告警走托盘气泡；跨线程只传普通数据，
  Qt 调用全留 UI 线程
- 启动位置钉死（评审必改）：`QuotaMonitor`/`BudgetAlerts` 类定义在 data_engine
  （不 import UI 模块），但只由 `MeterWindow` 实例化与启动；data_engine 自备
  `_no_persist()` 守卫（`ZM_NO_STATE=='1'` 或 `--verify`，同 `_state_guard` 语义）
  拦住 zm_alerts.json 写入与 monitor 启动；`run_all.py` data 组补注入 `ZM_NO_STATE=1`
  作第二道闸（仅加环境变量，断言未动）—— 带 key 的 zm_config.json 存在时回归
  也不发真实网络请求
- UI 细节：新增 燃速/套餐剩余 两行为卡片专属 label，横条与竖条两种形态都按纪律
  显式置 None（只在一种形态置 None 会让另一形态的压力循环摸已销毁 QLabel）；
  `CARD_H` 295→336（自然高度实测 314 + 多源行余量），`_restore_state`/
  `_detach_to_pointer`/`_unset_dock` 三处几何引用常量自动跟进（ui-verify 只打印
  尺寸不校验，需人工目视确认不截断）
- 安全与配置：`zm_config.json`（quota_api_key/daily_budget_cny/alert_pct）与
  `zm_prices.json`、`zm_alerts.json` 加入 `.gitignore`（key 提交风险专查项）；
  日志/调试路径不打印 key 明文；README 新增「配置」章节与口径表五行（金额三档
  规则/燃速窗口注记/计费块对齐与示意声明/多源口径差异/quota percentage 为已用%）
- 测试：新增 8 组单测（金额三路径与分组 SQL 手算对账、燃速窗口对账与 est 纯函数、
  计费块等分/总量/当前块/quota 锚、ClaudeSource 合成 jsonl 全规则、quota 解析、
  告警去重与跨日重置与持久化、ZCode 源=今日口径），全部无网络、不依赖真实
  ~/.claude；现有 8 个测试与断言一字未改

## v0.3.0 (2026-09-26) — 位置记忆 / 历史图表 / 手动切换 / 打包

- 位置记忆：退出与形态变化（贴边/拖离）即时保存 `x/y/贴边方向` 到 `zm_state.json`；
  启动恢复只还原位置与形态，宽高仍走常量 + `_refit_dock` 动态自愈（固定尺寸脱节的教训）；
  恢复时按"与保存点交集面积最大的屏"夹回可视区，完全离屏（改分辨率/拔显示器）回主屏默认位；
  `aboutToQuit` 兜菜单退出路径。强杀/断电丢最后纯拖动位置属可接受残余缺口
- 历史用量图表：独立窗口（普通 `Qt.Window`，带任务栏图标），按天（近 30 天，全来源
  in+out，同今日口径）/按会话（近 20 个，`main_turn`，与卡片逐字对齐）双页签 + 刷新；
  纯 QPainter 竖柱/水平条（不引 matplotlib，保 exe 体积）。两图口径不同、合计对不上账
  属预期（README 口径表已写明）
- 多会话手动切换：右键「会话」子菜单列出最近 8 个会话（part 最新写入倒序、排除
  subagent，与自动跟随同源），固定后卡片标题带 📌，「自动跟随(最近活跃)」恢复。
  重构 `_refresh_session` → `_switch_session`（自动/手动共用，序列原样保留：
  RLock→session_id→Snapshot 重建→`_last_len=None`→`_prev_max_rowid` 重置→title→
  poll→init_tps→push），并把 manual 检查与 `_latest_session` 查询整体包进
  `snap_lock`（RLock 可重入），消除「UI 固定与引擎 check-then-act」竞态
- 打包：PyInstaller 单文件命令与 frozen 注意事项写入 README「打包」章节；
  新增模块级 `app_dir()`（frozen 取 exe 目录），`zm_crash.log`/`zm_debug.log`/
  `zm_state.json` 在打包版落 exe 同目录；`faulthandler.enable` 包 try/except
  （只读目录不再启动即崩）；删除数据层遗留的 tkinter 死导入
- 测试：`ZM_NO_STATE=1` 隔离开关（ui/stress 子进程注入 + `--verify` 自检双守卫），
  回归不再改写用户真实位置记忆；新增 4 个口径单测（手动固定/恢复、最近会话排除
  subagent、按天=今日口径、按会话=main_turn 逐值对账）
- 已知边界：手动固定期间「生成中」状态/计时来自全局日志 tail 不区分会话（自动模式
  同源）；按天天界用 SQLite `localtime`，跨时区迁移时可能与 ZCode 自身统计差一天

## v0.2.0 (2026-09-26) — PySide6 版

- 架构迁移：tkinter → PySide6（`startSystemMove` 系统原生拖动，弃用全部 ctypes 拖动路径）
- 贴边胶囊条：宽度/高度按内容动态自适应（`_refit_dock` 周期校验，分辨率/DPI 变化自愈）
- 禁用 Aero Snap，防系统贴靠与变条逻辑打架
- 双速率：实时（流式估算+完成精确）+ 会话平均（按提供商+模型分组）
- 新增：首字等待(TTFT)、整体耗时、今日总用量、会话标题跟随
- 口径修正：
  - `input_tokens` 已含 `cache_read`（缓存率 = cache/in，不再双计）
  - 今日用量含全部来源（main_turn + subagent），与 ZCode 自身统计一致
  - 主力模型按 token 用量取（弃用字典序 MAX）
  - 活跃会话判定改 part 表最新写入行（rollout mtime 滞后一整轮）
- 会话切换：排除 subagent 劫持 + RLock 消除统计线程竞态
- 崩溃追踪：faulthandler → zm_crash.log；ZM_DEBUG=1 → zm_debug.log

## v0.1.0 (2026-09-25) — tkinter 版（留档）

- 首版：数据层（日志 tail + SQLite 轮询 + part 流式估算）、贴边条、DPI 适配
- 已知不可修的架构问题（触发迁移）：拖动迟滞、静置后吞点击、Win32 互操作崩溃
