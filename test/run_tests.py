#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-ocr 自检（self-contained：不依赖任何外部业务数据）

运行（需要一个能加载 RapidOCR 的 Python）：
    cd <plugin-dir>
    PYTHONPATH=core/_boot V3_EXTRA_SITE=<dsh-site-packages> python3 test/run_tests.py

覆盖：
  A. 数字清洗与页码解析 —— 纯函数，零依赖
  B. 合成图 text 模式 —— 现画一张已知文本的图，断言关键词与数字
  C. 合成合同图 clause 模式 —— 现画一张含条款关键词与日期的图，断言命中
  D. 合成表格图 table 模式 —— 现画一张两列、合计自洽的表，断言路由/列/行/一致性校验
  E.（可选）真实文档回归哨兵 —— 由环境变量指定，未设置则跳过

> 设计原则：**测试不引用任何私有路径或私有数据**。
> E 用例用于本机回归（真实扫描件），通过环境变量显式传入：
>     DSH_OCR_TEST_PDF=/abs/path/doc.pdf  DSH_OCR_TEST_PAGE=37
> 未设置时打印 SKIP，不算失败 —— 这样本插件在别的机器上也能完整自测。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
CORE = os.path.join(PLUGIN, "core")
sys.path.insert(0, CORE)

import ocr  # noqa: E402  (the engine under test)

PASS, FAIL, SKIP = [], [], []

# 可选的真实文档回归哨兵（由环境变量提供，绝不硬编码路径）
TEST_PDF = os.environ.get("DSH_OCR_TEST_PDF", "")
TEST_PAGE = int(os.environ.get("DSH_OCR_TEST_PAGE", "0") or 0)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s %s" % ("✅" if cond else "❌", name, ("· " + detail) if detail else ""))


def skip(name, why):
    SKIP.append(name)
    print("  ⏭  SKIP %s · %s" % (name, why))


def _font(size=40):
    """找一个可用的中文字体；找不到就退回默认字体（合成图可能读不准，会体现为失败）。"""
    from PIL import ImageFont
    for fp in ("/System/Library/Fonts/STHeiti Medium.ttc",
               "/System/Library/Fonts/Hiragino Sans GB.ttc",
               "/System/Library/Fonts/PingFang.ttc",
               "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
               "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
               "C:/Windows/Fonts/msyh.ttc"):
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:                      # noqa: BLE001
                continue
    return ImageFont.load_default()


def main():
    # ── A. 纯函数 ────────────────────────────────────────────────────────
    print("# A. 数字清洗 / 页码解析（纯函数）")
    check("千分位逗号 52,163", ocr.clean_number("52,163") == 52163.0,
          str(ocr.clean_number("52,163")))
    check("句点当千分位 52.163", ocr.clean_number("52.163") == 52163.0,
          str(ocr.clean_number("52.163")))
    check("括号负数 (1,234)", ocr.clean_number("(1,234)") == -1234.0,
          str(ocr.clean_number("(1,234)")))
    check("小数保留 3.14", abs(ocr.clean_number("3.14") - 3.14) < 1e-9,
          str(ocr.clean_number("3.14")))
    check("页码解析 1-3,5", ocr.resolve_pages("1-3,5", 10) == [1, 2, 3, 5],
          str(ocr.resolve_pages("1-3,5", 10)))

    engine = ocr.load_engine("ch")

    # ── B. 合成图 text 模式 ──────────────────────────────────────────────
    print("\n# B. 合成图 · text 模式")
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (1100, 160), "white")
        ImageDraw.Draw(img).text((30, 40), "合计金额 52,163 元", fill="black", font=_font(44))
        lines = ocr.run_page(engine, img)
        txt = ocr.page_text(lines)
        conf = round(sum(l["conf"] for l in lines) / len(lines), 3) if lines else 0.0
        check("合成图有识别行", len(lines) > 0, "%d 行" % len(lines))
        check("命中「合计」", "合计" in txt, txt[:40])
        check("命中 52,163 或 52163", "52,163" in txt or "52163" in txt, txt[:60])
        check("平均置信度 ≥ 0.9", conf >= 0.9, "conf=%s" % conf)
    except Exception as e:                          # noqa: BLE001
        check("合成图 text 测试", False, "异常: %s" % e)

    # ── C. 合成合同图 clause 模式 ───────────────────────────────────────
    print("\n# C. 合成合同图 · clause 模式")
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (1400, 380), "white")
        d = ImageDraw.Draw(img)
        f = _font(34)
        d.text((40, 40), "第七条 股权回购", fill="black", font=f)
        d.text((40, 110), "如公司未能在 2026年1月10日 前完成合格上市，", fill="black", font=f)
        d.text((40, 180), "投资方有权要求实际控制人回购其全部股权。", fill="black", font=f)
        d.text((40, 250), "第八条 反稀释", fill="black", font=f)
        lines = ocr.run_page(engine, img)
        clauses = ocr.clause_hits([(1, lines)])
        types = {c["clause_type"] for c in clauses}
        joined = "".join(" ".join(c.get("quotes", [])) for c in clauses)
        check("命中「回购」类条款", "回购" in types, str(sorted(types)))
        check("关键日期进入引用", "2026年1月10日" in joined or "1月10日" in joined, "")
    except Exception as e:                          # noqa: BLE001
        check("合成合同 clause 测试", False, "异常: %s" % e)

    # ── D. 合成表格图 · table 模式（路由 + 列 + 行 + 一致性校验）─────────
    print("\n# D. 合成表格图 · table 模式")
    try:
        from PIL import Image, ImageDraw
        # 两列数值 + 合计自洽：1000 = 600 + 400；900 = 500 + 400
        rows = [("资产总计", "1,000", "900"),
                ("负债合计", "600", "500"),
                ("所有者权益合计", "400", "400")]
        img = Image.new("RGB", (1400, 460), "white")
        d = ImageDraw.Draw(img)
        f = _font(36)
        # 数值**右对齐**（真实财务表都是右对齐；左对齐会让右缘参差，
        # 几何重建的「右缘聚类」会把一列劈成两列 —— 这是本用例要覆盖的真实约束）
        RIGHT_A, RIGHT_B = 980, 1300

        def _rtext(x_right, y, text):
            d.text((x_right - d.textlength(text, font=f), y), text, fill="black", font=f)

        d.text((60, 60), "项目", fill="black", font=f)
        _rtext(RIGHT_A, 60, "期末")
        _rtext(RIGHT_B, 60, "期初")
        y = 140
        for label, a, b in rows:
            # 每格单独绘制 ⇒ OCR 会得到独立的框（几何重建依赖这一点）
            d.text((60, y), label, fill="black", font=f)
            _rtext(RIGHT_A, y, a)
            _rtext(RIGHT_B, y, b)
            y += 90
        te = ocr.table_extract(engine, img)
        route = te.get("route") or {}
        check("table 抽取成功", bool(te.get("ok")), str(te.get("reason") or "")[:60])
        check("检测到 2 个数值列", te.get("columns") == 2, "columns=%s" % te.get("columns"))
        check("路由有决策记录", bool(route.get("decision")),
              "decision=%s" % route.get("decision"))
        # ★ 必须走**几何重建**：两列右对齐表若落到切条法，数字会被从中间切开
        #   （实测切条把 900 切成 90+0）。这条断言专门防「geo 静默失败被切条掩盖」。
        check("走几何重建（非切条）", route.get("decision") == "geometry",
              "decision=%s reason=%s" % (route.get("decision"),
                                         str(route.get("reason"))[:70]))
        labels = [re.sub(r"[\s·．.：:]+", "", r["label"]) for r in (te.get("rows") or [])]
        check("有「资产总计」行", "资产总计" in labels, str(labels)[:80])
        check("有「负债合计」行", "负债合计" in labels, "")
        check("有「所有者权益合计」行", "所有者权益合计" in labels, "")
        checks = te.get("balance_checks") or []
        ok_bal = len(checks) >= 2 and all(c["balanced"] for c in checks)
        check("表内一致性校验通过", ok_bal, json.dumps(checks, ensure_ascii=False)[:160])
    except Exception as e:                          # noqa: BLE001
        check("合成表格 table 测试", False, "异常: %s" % e)

    # ── E. 可选：真实文档回归哨兵 ────────────────────────────────────────
    print("\n# E. 真实文档回归哨兵（可选）")
    if TEST_PDF and TEST_PAGE and os.path.exists(TEST_PDF):
        try:
            images = ocr.load_images(TEST_PDF, [TEST_PAGE], 300)
            te = ocr.table_extract(engine, images[0][1])
            route = te.get("route") or {}
            print("    route=%s columns=%s rows=%s" % (
                route.get("decision"), te.get("columns"), len(te.get("rows") or [])))
            check("真实文档 table 抽取成功", bool(te.get("ok")), str(te.get("reason") or "")[:60])
            checks = te.get("balance_checks") or []
            if checks:
                check("真实文档一致性校验全平", all(c["balanced"] for c in checks),
                      json.dumps(checks, ensure_ascii=False)[:120])
            else:
                skip("真实文档一致性校验", "该页无表内合计行可比（如续页），跳到一致性断言")
        except Exception as e:                      # noqa: BLE001
            check("真实文档回归", False, "异常: %s" % e)
    else:
        skip("真实文档回归哨兵",
             "未设置 DSH_OCR_TEST_PDF / DSH_OCR_TEST_PAGE（本机回归时用）")

    print("\n# 结果：%d 通过 / %d 失败 / %d 跳过" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
