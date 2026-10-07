#!/usr/bin/env python3
"""zcode-meter 回归测试套件。

用法:
  python tests/run_all.py            # 跑全部
  python tests/run_all.py data ui    # 只跑指定组

组:
  data  数据层单测(口径/会话切换/subagent排除) —— 无 UI,快
  pkg   包结构守卫(v0.6.0 src 布局:包可导入/版本号对账/零副作用红线/shim 链路)
  ui    界面自检(--verify 封装:渲染/绑定/贴边判定)
  stress 布局切换压力测试(横条<->竖条<->卡片 + 动态refit)
  glass 玻璃管线单测(test_glass_effect:锁序/退避/纯逻辑,monkeypatch 注入,
        不依赖 dxcam 安装与真实屏幕)
  cli   CLI 单测(test_cli:故障通道 rc=1/显示宽度/≈注脚/boot_queries,
        monkeypatch DB_PATH 指向 tmp,不触真实 ~/.zcode)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# UI 相关子进程禁用位置记忆:防回归测试改写用户真实 zm_state.json
# (拦 _set_dock/_unset_dock 即时保存与 aboutToQuit 落盘两条写脏路径)。
# v0.4.0 起 data 组同样注入:兼作 QuotaMonitor/zm_alerts.json 的网络与
# 落盘守卫(见 main() 内注释)。
STATE_OFF = dict(os.environ, ZM_NO_STATE="1")


def run(name: str, cmd: list[str], timeout: int = 90,
        env: dict | None = None) -> tuple[str, bool, str]:
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=env)
        out = (p.stdout + p.stderr).strip()
        ok = p.returncode == 0
    except subprocess.TimeoutExpired:
        out, ok = "TIMEOUT", False
    dt_ = time.time() - t0
    print(f"\n[{name}] {'PASS' if ok else 'FAIL'}  ({dt_:.1f}s)")
    tail = "\n".join(out.splitlines()[-6:])
    if tail:
        print("  " + tail.replace("\n", "\n  "))
    return name, ok, out


def main() -> int:
    # v0.9.2:默认组扩到六条(data/pkg/ui/stress/glass/cli)—— glass/cli 两新
    # 测试文件是本批新增回归面,不进默认组则「全绿」会静默漏跑(同 pkg 组教训)
    groups = sys.argv[1:] or ["data", "pkg", "ui", "stress", "glass", "cli"]
    results = []

    if "data" in groups:
        # v0.4.0:data 组也注入 STATE_OFF —— 第二道闸(评审必改#1):data_engine
        # 的 QuotaMonitor/BudgetAlerts 守卫读 ZM_NO_STATE,补注入保证带 key 的
        # zm_config.json 存在时,data 单测路径同样绝不发网络请求、不写
        # zm_alerts.json。仅加环境变量,断言与超时一字未动。
        results.append(run("data-engine", [sys.executable, str(ROOT / "tests" / "test_data_engine.py")],
                           env=STATE_OFF))

    if "pkg" in groups:
        # v0.6.0:src 布局迁移守卫 —— 不在默认组里加这条命令,「全绿」会静默漏跑
        results.append(run("pkg", [sys.executable, str(ROOT / "tests" / "test_package.py")],
                           env=STATE_OFF))

    if "ui" in groups:
        results.append(run("ui-verify",
                           [sys.executable, str(ROOT / "src" / "zcode_meter" / "app.py"), "--verify"],
                           env=STATE_OFF))

    if "stress" in groups:
        results.append(run("layout-stress", [sys.executable, str(ROOT / "tests" / "test_stress.py")],
                           env=STATE_OFF))

    if "glass" in groups:
        # v0.9.2:玻璃管线单测进默认组 —— 全程 monkeypatch 注入,无 dxcam/
        # 真实屏幕依赖,任何环境都可跑(测试文件自身保证,这里只负责注册)
        results.append(run("glass-effect", [sys.executable, str(ROOT / "tests" / "test_glass_effect.py")],
                           env=STATE_OFF))

    if "cli" in groups:
        # v0.9.2:CLI 单测进默认组 —— DB_PATH monkeypatch 指向 tmp 合成库,
        # 不触真实 ~/.zcode;含故障通道 rc=1/boot_queries 连接计数等断言
        results.append(run("cli", [sys.executable, str(ROOT / "tests" / "test_cli.py")],
                           env=STATE_OFF))

    print("\n" + "=" * 46)
    all_ok = all(ok for _, ok, _ in results)
    for name, ok, _ in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    print("=" * 46)
    print("RESULT:", "ALL PASS" if all_ok else "FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
