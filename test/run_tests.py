#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-ocr 自检：用「M1 模块 v3 遇到的问题」跑三类用例。

运行（需能加载 RapidOCR 的解释器，Anaconda 或 v3py）：
    cd <materials-root>
    ./toolchain/v3py <plugin-dir>/dsh-ocr/test/run_tests.py
    # 或
    /opt/anaconda3/bin/python3 <plugin-dir>/dsh-ocr/test/run_tests.py

覆盖：
  A. 数字清洗（逗号/句点混淆、千分位、括号负数）—— 纯函数，M1 踩过的坑
  B. 合成图 text 模式（断言关键词命中 + conf 阈值）
  C. 合同条款 clause 模式（P-004 增资协议 p9，断言「回购」命中 + 关键日期）
  D. 真实扫描件 FDD table 模式（<项目A> A04-1 p37 资产负债表，断言 BS 平衡勾稽 3/3）
"""
import os
import sys
import json
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
CORE = os.path.join(PLUGIN, "core")
sys.path.insert(0, CORE)

import ocr  # noqa: E402  (the engine under test)

ROOT = os.path.expanduser("<materials-root>")
FDD = os.path.join(ROOT, "项目材料", "<项目A>",
                   "关于提请审议投资<项目A>科技股份有限公司项目决策的议案",
                   "项目决策会议案提交单", "尽职调查报告终版（决策）",
                   "A04-1 <项目A>股权投资项目财务尽职调查报告.pdf")
CONTRACT_PAGE = os.path.join(ROOT, "m3", "ocr", "pages", "<contract-page>.png")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s %s" % ("✅" if cond else "❌", name, ("· " + detail) if detail else ""))


def main():
    print("# A. 数字清洗（M1 踩过的逗号/句点混淆）")
    check("千分位逗号 52,163", ocr.clean_number("52,163") == 52163.0, str(ocr.clean_number("52,163")))
    check("句点当千分位 52.163", ocr.clean_number("52.163") == 52163.0, str(ocr.clean_number("52.163")))
    check("括号负数 (1,234)", ocr.clean_number("(1,234)") == -1234.0, str(ocr.clean_number("(1,234)")))
    check("小数保留 3.14", abs(ocr.clean_number("3.14") - 3.14) < 1e-9, str(ocr.clean_number("3.14")))
    check("页码解析 1-3,5", ocr.resolve_pages("1-3,5", 10) == [1, 2, 3, 5], str(ocr.resolve_pages("1-3,5", 10)))

    engine = ocr.load_engine("ch")

    print("\n# B. 合成图 text 模式")
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (900, 160), "white")
        d = ImageDraw.Draw(img)
        font = None
        for fp in ("/System/Library/Fonts/STHeiti Medium.ttc",
                   "/System/Library/Fonts/Hiragino Sans GB.ttc",
                   "/System/Library/Fonts/PingFang.ttc"):
            if os.path.exists(fp):
                try:
                    font = ImageFont.truetype(fp, 40)
                    break
                except Exception:
                    font = None
        if font is None:
            font = ImageFont.load_default()
        d.text((30, 40), "资产负债表 货币资金 52,163 千元", fill="black", font=font)
        lines = ocr.run_page(engine, img)
        txt = ocr.page_text(lines)
        conf = round(sum(l["conf"] for l in lines) / len(lines), 3) if lines else 0.0
        check("合成图有识别行", len(lines) > 0, "%d 行" % len(lines))
        check("命中「货币资金」", "货币资金" in txt, txt[:60])
        check("命中数字 52,163 或 52163", "52,163" in txt or "52163" in txt, txt[:80])
        print("    conf=%s text=%r" % (conf, txt[:80]))
    except Exception as e:
        check("合成图测试", False, "异常: %s" % e)

    print("\n# C. 合同条款 clause 模式（P-004 增资协议 p9）")
    if os.path.exists(CONTRACT_PAGE):
        from PIL import Image
        img = Image.open(CONTRACT_PAGE).convert("RGB")
        lines = ocr.run_page(engine, img)
        clauses = ocr.clause_hits([(1, lines)])
        types = {c["clause_type"] for c in clauses}
        check("命中「回购」", "回购" in types, str(types))
        joined = "".join(" ".join(c.get("quotes", [])) for c in clauses)
        check("关键日期 2026年1月10日 在引用里", "2026年1月10日" in joined or "1月10日" in joined, "")
        print("    条款类型=%s" % sorted(types))
    else:
        check("合同页存在", False, "缺 %s" % CONTRACT_PAGE)

    print("\n# D. 真实扫描件 FDD table 模式（<项目A> A04-1 p37 资产负债表）")
    if os.path.exists(FDD):
        images = ocr.load_images(FDD, [37], 300)
        img = images[0][1]
        te = ocr.table_extract(engine, img)
        check("检测到 3 个数值列", te.get("columns") == 3, "columns=%s" % te.get("columns"))
        labels = [r["label"] for r in te.get("rows", [])]
        norm = [re.sub(r"[\s·．.：:]+", "", l) for l in labels]
        check("有「资产总计」", any(l == "资产总计" for l in norm), "")
        check("有「负债合计」", any(l == "负债合计" for l in norm), "")
        check("有「所有者权益合计」", any(l == "所有者权益合计" for l in norm), "")
        checks = te.get("balance_checks", [])
        all_bal = len(checks) >= 3 and all(c["balanced"] for c in checks)
        check("BS 平衡勾稽 3/3 通过（diff=0）", all_bal, json.dumps(checks, ensure_ascii=False))
        for c in checks:
            print("    col%d: 资产总计 %s vs 负债+权益 %s → %s" % (
                c["col"], c["assets_total"], c["liab_plus_equity"], "✓" if c["balanced"] else "✗ diff=" + str(c["diff"])))
    else:
        check("FDD 存在", False, "缺 %s" % FDD)

    print("\n# 结果：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
