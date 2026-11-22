#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-ocr 体检：一次性检查「工具消失 / 签名失效 / 依赖缺失」三类高频问题。

背景：2026-10-22 出现 dsh-ocr 在 dependencies 里但不在 dsh.profile.bundles，
导致 doc_ocr / doc_preprocess 工具消失（插件装了没挂载）。

检查项：
  A. 内置 Python 签名（ad-hoc + disable-library-validation）
  B. 原生扩展 import（onnxruntime / cv2 / pypdfium2）
  C. 插件包完整性（package.json / cordis.patch.yml / index.js / core 文件）
  D. profile 注册（dependencies + dsh.profile.bundles 都有 dsh-ocr）
  E. 冒烟：对一张合同页跑 text 模式，conf ≥ 0.9

运行：
  python3 test/health_check.py
退出码：0 全绿；1 有 FAIL（会打印修复建议）。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PY_DEFAULT = os.path.expanduser(
    "~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3")
PROFILE_PKG = os.path.expanduser("~/.dsh/profiles/desktop/package.json")
CONTRACT = os.path.expanduser("<materials-root>/m3/ocr/pages/<contract-page>.png")

PASS, FAIL = [], []


def check(name, cond, detail="", fix=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s %s" % ("✅" if cond else "❌", name, ("· " + detail) if detail else ""))
    if not cond and fix:
        print("     修复：", fix)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main():
    py = os.environ.get("DSH_OCR_PYTHON", PY_DEFAULT)
    print("# dsh-ocr 体检")
    print("# interpreter:", py)

    print("\n# A. 内置 Python 签名")
    if os.path.exists(py):
        r = run(["codesign", "-dvvv", "--entitlements", "-", py])
        txt = (r.stdout or "") + (r.stderr or "")
        adhoc = "flags=0x" in txt and "adhoc" in txt
        dlv = "com.apple.security.cs.disable-library-validation" in txt
        check("ad-hoc 签名", adhoc)
        check("disable-library-validation", dlv,
              fix='doc_ocr_sign_fix(mode="auto")')
    else:
        check("解释器存在", False, py, fix="设 DSH_OCR_PYTHON 或重装 DSH 运行时")

    print("\n# B. 原生扩展 import")
    r = run([py, "-c", "import onnxruntime, cv2, pypdfium2; print('OK')"])
    check("onnxruntime/cv2/pypdfium2", r.returncode == 0,
          (r.stdout + r.stderr).strip().splitlines()[-1][:80] if (r.stdout + r.stderr).strip() else "",
          fix='doc_ocr_sign_fix(mode="auto") 后重试')

    print("\n# C. 插件包完整性")
    for rel in ("package.json", "cordis.patch.yml", "index.js", "runner.js",
                "core/ocr.py", "core/pipeline.py", "core/workpaper.py",
                "core/jev.py"):
        check(rel, os.path.exists(os.path.join(PLUGIN, rel)))
    try:
        pkg = json.load(open(os.path.join(PLUGIN, "package.json")))
        check("package.json 可解析", True, "v%s" % pkg.get("version"))
        check("dsh.bundle.patch", bool(pkg.get("dsh", {}).get("bundle", {}).get("patch")))
    except Exception as e:
        check("package.json 可解析", False, str(e))

    print("\n# D. profile 注册（工具消失的头号原因）")
    try:
        prof = json.load(open(PROFILE_PKG))
        deps = prof.get("dependencies", {})
        bundles = prof.get("dsh", {}).get("profile", {}).get("bundles", [])
        check("dependencies 含 dsh-ocr", "dsh-ocr" in deps)
        check("bundles 含 dsh-ocr", "dsh-ocr" in bundles,
              fix='plugin_manager install_bundle(target=插件目录)；若报 already-installed，先 remove_bundle 再 install_bundle，然后重启 DSH')
        check("dependencies 含 dsh-macos-signfix", "dsh-macos-signfix" in deps)
        check("bundles 含 dsh-macos-signfix", "dsh-macos-signfix" in bundles)
    except Exception as e:
        check("profile package.json 可读", False, str(e))

    print("\n# E. 冒烟（text 模式）")
    if os.path.exists(CONTRACT):
        r = run([py, os.path.join(PLUGIN, "core", "ocr.py"),
                 "--input", CONTRACT, "--mode", "text", "--dpi", "200"], timeout=120)
        try:
            d = json.loads(r.stdout)
            p = d["pages"][0]
            check("引擎 ok", d.get("ok") is True)
            check("conf ≥ 0.9", (p.get("avg_conf") or 0) >= 0.9, "conf=%s" % p.get("avg_conf"))
        except Exception:
            check("引擎 ok", False, (r.stdout + r.stderr)[-160:])
    else:
        check("合同页存在", False, CONTRACT)

    print("\n# 结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
