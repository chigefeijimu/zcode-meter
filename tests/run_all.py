#!/usr/bin/env python3
"""zcode-meter 回归测试套件。

用法:
  python tests/run_all.py            # 跑全部
  python tests/run_all.py data ui    # 只跑指定组

组:
  data  数据层单测(口径/会话切换/subagent排除) —— 无 UI,快
  ui    界面自检(--verify 封装:渲染/绑定/贴边判定)
  stress 布局切换压力测试(横条<->竖条<->卡片 + 动态refit)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# UI 相关子进程禁用位置记忆:防回归测试改写用户真实 zm_state.json
# (拦 _set_dock/_unset_dock 即时保存与 aboutToQuit 落盘两条写脏路径;
#  data 组无 UI 不需要)
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
    groups = sys.argv[1:] or ["data", "ui", "stress"]
    results = []

    if "data" in groups:
        results.append(run("data-engine", [sys.executable, str(ROOT / "tests" / "test_data_engine.py")]))

    if "ui" in groups:
        results.append(run("ui-verify", [sys.executable, str(ROOT / "zcode_meter_qt.py"), "--verify"],
                           env=STATE_OFF))

    if "stress" in groups:
        results.append(run("layout-stress", [sys.executable, str(ROOT / "tests" / "test_stress.py")],
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
