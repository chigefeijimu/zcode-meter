/* zcode-workflow
description: zcode-meter 插件的迭代开发流水线：解析需求→评审计划→实现→自动回归(run_all)→产出验收清单与迭代报告
whenToUse: 要对 zcode-meter 桌面小工具做任何迭代时使用——修 bug、加统计指标、调 UI 交互。传入本次迭代需求描述即可。
args:
  request:
    type: string
    description: 本次迭代的需求描述：要修的 bug 现象（含复现步骤）、或要加的功能与期望口径
*/
interface Impact {
  /** 本次改动落在哪一层:data_engine.py 数据层 / zcode_meter_qt.py 界面层 / 两者 */
  affected: "data" | "ui" | "both";
  /** 实现步骤,每步一句话,按顺序执行 */
  plan: string[];
  /** 可能破坏的既有行为(口径/交互),供评审重点检查 */
  risks: string[];
}

interface Review {
  /** 计划是否可以直接执行 */
  approved: boolean;
  /** 不通过时:具体问题与修改建议;通过时:一句话说明为何安全 */
  feedback: string;
}

interface ImplResult {
  /** 一两句话:改了什么、怎么改的 */
  summary: string;
  /** 本次修改的文件(相对 workspace) */
  changedFiles: string[];
  /** 建议写入 CHANGELOG.md 的条目(含口径说明) */
  changelogEntry: string;
  /** 需要人工在桌面实测的验收点(如:贴边四方向/拖离/切换会话) */
  manualChecks: string[];
}

const REQ = String(args.request ?? "").trim();
if (!REQ) {
  return { conclusion: "未提供迭代需求(request 参数为空),本次无事可做。", findings: [], verified: [], notCovered: ["一切 — 需求为空"] };
}

const PROJ = "zcode-meter";

phase("理解需求并分析影响面");
const analyst = agent("需求分析师", {
  system: "你负责分析 zcode-meter(PySide6 桌面小工具)的迭代需求并产出实现计划。" +
    "先读 " + PROJ + "/README.md 的『显示指标与口径』表 — 口径是这个项目的命根子,历史上多数 bug 是口径错误。" +
    "再读 " + PROJ + "/CHANGELOG.md 了解口径演变。按需求所指读 " + PROJ + "/data_engine.py(数据层)或 " + PROJ + "/zcode_meter_qt.py(界面层)的相关代码。" +
    "若需求涉及异常/崩溃/交互失灵:读 " + PROJ + "/zm_debug.log 与 " + PROJ + "/zm_crash.log(存在时)做归因。" +
    "输出计划要具体到函数级;risks 必须列出可能被破坏的既有口径或交互。",
});
const impact = await analyst.ask<Impact>("本次迭代需求:\n" + REQ);

phase("独立评审实现计划");
const reviewer = agent("计划评审员", {
  system: "你独立评审一份针对 zcode-meter 的改动计划。读计划将触碰的源码文件验证可行性,但不要编辑任何文件。" +
    "重点问:会破坏 README 口径表的哪一条?会不会重蹈 CHANGELOG 里记录的历史覆辙(如 input 双计 cache、subagent 污染会话判定、固定尺寸脱节、回调悬空引用)?" +
    "计划有缺口就拒绝并给出具体修改意见;批准必须给出理由。",
});
let plan = impact;
let approved = false;
for (let round = 0; round < 2 && !approved; round++) {
  const rev = await reviewer.ask<Review>("评审这份计划(第 " + (round + 1) + " 轮):\n" + JSON.stringify(plan));
  approved = rev.approved;
  if (!approved) {
    plan = await analyst.ask<Impact>("评审员意见如下,请修订计划:\n" + rev.feedback);
  }
}

phase("按计划实现改动");
const impl = agent("实现工程师", {
  system: "你是资深 Python/PySide6 工程师,在 zcode-meter 项目上实现已评审的计划。" +
    "遵守项目惯例:注释写清『为什么』;数据层改动注意 SQLite 连接生命周期(历史 bug:查询写在 con.close 之后被异常吞噬);" +
    "界面层改动注意动态尺寸自愈(_refit_dock)与布局切换后的悬空引用。" +
    "回归测试由外部脚本统一运行,你不要自己跑 run_all.py。" +
    "若发现计划有误或指令自相矛盾,直说并停止,不要绕过。",
});
const result = await impl.ask<ImplResult>(
  "按以下已评审计划实现(需求:" + REQ + "):\n" + JSON.stringify(plan) + "\n" +
    "完成后输出 summary/changedFiles/changelogEntry/manualChecks。manualChecks 列出必须人工在桌面实测的交互点。",
);

phase("自动回归直到全绿");
let pass = false;
let lastOutput = "";
for (let round = 1; round <= 3; round++) {
  const t = await world.run("python", [PROJ + "/tests/run_all.py"], { timeoutMs: 300000 });
  pass = t.exitCode === 0;
  lastOutput = (t.stdout + "\n" + t.stderr).slice(-4000);
  log("回归第 " + round + " 轮:" + (pass ? "全部通过" : "有失败"));
  if (pass) break;
  await impl.ask("回归失败(第 " + round + " 轮),修复后仍不要自己重跑:\n" + lastOutput);
}

phase("产出迭代报告");
const reportMd = [
  "# zcode-meter 迭代报告",
  "",
  "## 需求",
  REQ,
  "",
  "## 改动",
  result.summary,
  "",
  "## 变更文件",
  ...result.changedFiles.map((f) => "- " + f),
  "",
  "## CHANGELOG 建议",
  "> " + result.changelogEntry,
  "",
  "## 人工验收清单",
  ...result.manualChecks.map((c, i) => String(i + 1) + ". " + c),
  "",
  "## 回归",
  pass ? "tests/run_all.py 全部通过" : "回归未通过(3 轮后仍有失败),见结论",
].join("\n");
await artifact.markdown("iteration-report", reportMd, {
  title: "zcode-meter 迭代报告",
  description: result.summary.slice(0, 200),
  primary: true,
});

return {
  conclusion: pass
    ? "迭代完成并通过自动回归: " + result.summary + " 人工验收清单见报告。"
    : "实现完成但自动回归 3 轮未通过,不要上线,先看报告中的失败详情。最后输出:\n" + lastOutput,
  findings: result.changedFiles.map((f) => ({
    where: f,
    what: result.summary,
    evidence: "评审通过的计划: " + plan.plan.join("; "),
    status: pass ? "verified" : "unconfirmed",
    severity: pass ? "low" : "high",
  })),
  verified: pass ? ["python " + PROJ + "/tests/run_all.py 退出码 0"] : [],
  notCovered: ["桌面端真实交互(拖动/贴边/切换)需按人工验收清单实测 — 自动化无法覆盖 DPI 窗口的真实鼠标输入"],
};
