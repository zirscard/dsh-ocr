#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""geo_table.py —— **几何表格重建**（只用 OCR 精确 bbox，不再切图重 OCR）

【为什么需要（2026-11-14 实测驱动）】
现有 `table` 模式走「切条重 OCR」：把页面按列边界切成竖条 → 每条单独 OCR → 归行。
**切条边界会落进数字中间**。实测<项目B> A05-2 审计报告 p6（合并资产负债表）：

    label=货币资金  cells=[142338049.42, 67654.0, 174.9]
                              ↑ 67654174.90 被切成 67654.0 + 174.9

根因链（全部实测）：
  ① `_columns_from_tokens()` **把列数硬编码为 3**（注释写明「已验证口径：FDD p37 三线表」），
     而这份 BS 只有 **2 个数值列** ⇒ k-means 强行劈出 3 列 ⇒ 切条边界落进数字中间。
  ② 历史修法失败的原因是 token 跨度用**字符比例估算**（不精确），k=6 时会把 "608,209" 拆成 608/209。
  ③ 但 **RapidOCR 本来就给出每个文本框的精确 bbox**（`run_page` 已输出 x/y/w/h 归一化坐标）
     —— 根本不需要切图重 OCR，**直接用框坐标做列聚类与归行即可，永不截断数字**。

【算法：数值锚 + 区间重叠归行】
  ① **列**：全部数值框按**右缘**的间隙聚类（右对齐 ⇒ 右缘稳定；不用宽度缩放阈值）。
  ② **行锚 = 数值**：每个数值框自己是一个「逻辑行」的锚（同一行的多个数值自然靠 y 聚成一组）。
  ③ **归标签**：标签归给**纵向区间与之重叠**的数值行；
     若不重叠，则归给**距离最近且在其上方**的数值行（允许跨过无金额的科目行）。
  ④ **列名**：位于首个数值行**上方**、且 x 区间与该列重叠的标签 = 该列表头。
  ⑤ **分栏**：标签 x 明显两簇且数值列也能分开 ⇒ 切左右两栏（双栏 BS）。

【★ 踩坑史（这些都试过并失败，勿重走）】
  2026-11-14 试过 6 轮，全部失败：
    · 「标签顶 ≤ 数值顶」⇒ 漏掉「营业收入」（标签行本身有高度，标签顶比数值顶更高）
    · 「标签底 ≥ 数值顶」⇒ 挂到**下一行**
    · 「中心差最小」⇒ 仍挂到下一行
    · 「允许跨无金额科目行回溯」⇒ 回溯到**表头**（「项日」= 表头 OCR 错字吃掉营业收入）
    · 「按 y 中心聚类成行」⇒ 同行的标签与数值被**拆到不同行**
      （实测 A05-2 p6：货币资金 yc=0.1500、金额 yc=0.1731，**差 0.023 ≈ 1.5 个框高**
        ⇒ OCR 把同一视觉行给了**不同的 y 带**，聚类参数怎么调都不稳）
    · 「按间隙双峰定行边界」⇒ 同样被 y 带散布破坏
  **共同教训**：**不要试图把 OCR 框还原成视觉行**（y 带不可靠）；
    要用**财务表的强结构**：数值是硬锚（每个数自带一个可信的行位置），
    标签按「与该数值的纵向关系」挂上去。

【用法】
    from geo_table import reconstruct
    rows = reconstruct(lines, clean_number=clean_number, num_re=NUM_RE)
"""
import re

CJK = re.compile(r"[一-鿿]")

# 附注引用：形如「(十四)」「七、(一)」「七、」—— 它是**附注列**，不是行标签
#   （2026-11-14 实测：项目C A05-2 p4 的附注号带项号前缀「七、(一)」）
FOOTNOTE_PART = re.compile(r"[（(][一二三四五六七八九十百]+[)）]|[一二三四五六七八九十百]+[、，]")


def _strip_footnote(text):
    """剥掉附注段，返回剩余的科目名部分。"""
    return FOOTNOTE_PART.sub("", text or "").strip(" 　,，、:：;；")


def _is_footnote_only(text):
    """整行是否只剩附注内容（无真科目名）。"""
    return len(CJK.findall(_strip_footnote(text))) < 2


def _numeric_value(text, clean_number):
    """整行文本是否就是一个数。返回数值或 None。

    刻意**只接受整行即数字**的行：混合行（标签 + 数字）不做字符比例估算
    （2026-11-14 实测：字符比例估算会把 "608,209" 拆成 608/209）。
    """
    t = (text or "").strip()
    if not t or not re.search(r"\d", t):
        return None
    if not re.fullmatch(r"[\s\d,.()（）\[\]%\-－–—+*¥￥$€]*\d[\s\d,.()（）\[\]%\-－–—+*¥￥$€]*", t):
        return None
    return clean_number(t)


def _boxes(lines):
    out = []
    for l in lines or []:
        t = str(l.get("text") or "").strip()
        if not t:
            continue
        x0 = l["x"]; x1 = l["x"] + l["w"]
        y0 = l["y"]; y1 = l["y"] + l["h"]
        out.append({"text": t, "x0": x0, "x1": x1, "cx": (x0 + x1) / 2.0,
                    "y0": y0, "y1": y1, "yc": (y0 + y1) / 2.0, "h": l.get("h", 0.015),
                    "conf": l.get("conf")})
    return out


MIN_TOKENS_PER_COL = 2      # 每列最少 token 数（**唯一阈值**，khlib 侧也用它）


def cluster_columns(num_boxes, min_gap=0.018, min_tokens=MIN_TOKENS_PER_COL):
    """数值框 → 列簇，并剔除**伪列**（token 数不足的列）→ (cols, pseudo)。

    ★ 2026-11-15 评审修正：此前 khlib 的 `parse_classify` **复制**了一份聚类，
      且「每列最少 token 数」阈值不同（khlib ≥3 / 此处 ≥2）⇒ 同一页两侧给出
      **相反**的分栏结论（实测：插件判双栏、khlib 判单栏）。
      ⇒ 现在只有这一份实现、这一个阈值；khlib 侧直接调用本函数。
    """
    cols = _cluster_columns(num_boxes, min_gap=min_gap)
    keep, pseudo = [], []
    for c in cols:
        if len(c["tokens"]) >= min_tokens:
            keep.append(c)
        else:
            for t in c["tokens"]:
                pseudo.append({"text": t.get("text", ""), "v": t.get("v"),
                               "x": round(t.get("cx", 0), 4),
                               "y": round(t.get("y0", 0), 4),
                               "reason": "伪列：仅 %d 个 token 支撑" % len(c["tokens"])})
    return keep, pseudo


def _cluster_columns(num_boxes, min_gap=0.018):
    """按**右缘**（x1）的间隙聚类成列。

    为什么用右缘：财务数字**右对齐**，同一列的右缘几乎重合
    （实测 A05-2 p6：期末列右缘 0.716/0.715/0.713，上年列 0.843），
    而左缘随数字长度漂移。阈值用**绝对间隙**（不用宽度缩放：宽数字会撑大阈值吃掉真列）。
    """
    if not num_boxes:
        return []
    ordered = sorted(num_boxes, key=lambda b: b["x1"])
    groups = [[ordered[0]]]
    for b in ordered[1:]:
        if b["x1"] - groups[-1][-1]["x1"] > min_gap:
            groups.append([b])
        else:
            groups[-1].append(b)
    cols = []
    for g in groups:
        cols.append({"left": min(b["x0"] for b in g),
                     "right": max(b["x1"] for b in g),
                     "center": sum(b["cx"] for b in g) / len(g),
                     "tokens": g})
    return cols


def _group_numeric_rows(cols, tol=0.011):
    """把各列的数值按 y **聚成逻辑行**（同一行 = 各列各一个数）。

    容差 tol：数值框的 y 中心在同一行内差 ≤0.005（实测 142,338,049.42 yc=0.1731 /
    67,654,174.90 yc=0.1741）⇒ 0.011 足够；行与行之间间隔 ≥0.016 ⇒ 不会被合并。
    """
    items = []
    for j, c in enumerate(cols):
        for t in c["tokens"]:
            items.append((t["yc"], j, t))
    items.sort(key=lambda z: z[0])
    groups = []
    for yc, j, t in items:
        if groups and abs(yc - groups[-1]["yc"]) <= tol:
            groups[-1]["members"].append((j, t))
            groups[-1]["yc"] = sum(m[1]["yc"] for m in groups[-1]["members"]) / len(groups[-1]["members"])
        else:
            groups.append({"yc": yc, "members": [(j, t)]})
    return groups


def detect_panels(cols, min_cols_per_side=2):
    """把数值列簇切成**两栏**（双栏表：左资产 / 右负债权益）。

    ★ 判据（2026-11-15 实测 5 样本 + 评审修正定稿）：
      ① 间隙用**右缘**（与 `_cluster_columns` 的聚类口径一致）——
         用中心点量间隙会错：中心点随数字位数漂移，
         右边一列数字很宽时，最大间隙会落到**栏内部**（评审实测：center 间隙
         [0.12,0.14,0.29] vs right 间隙 [0.12,0.28,0.15]）。
      ② 在**最大间隙**处切分，要求**两侧各有 ≥min_cols_per_side 个列簇**
         —— 这是主判据，实测 5 样本全对：
           <项目A> p37（双栏）左3|右3 ✅｜FDD p37 左3|右1 ✗单栏 ✅
           FDD p20 左1|右3 ✗单栏 ✅｜FDD p12 左1|右2 ✗单栏 ✅｜审计 p6 左1|右1 ✗单栏 ✅
      ③ 加强条件：组间间隙须 > 组内**最大**间隙 ×1.2
         （用中位数×2.5 会漏判「栏内列距大」的真双栏：
          真双栏 0.10/0.25 | 0.60/0.80 ⇒ 0.35 vs 中位 0.20 ⇒ 被误判单栏）

    返回 (split_at | None, evidence)。
    evidence 里的字段是**可断言的**（回归测试直接断言它）。
    """
    ev = {"cols": len(cols), "split_at": None, "left": 0, "right": 0,
          "max_gap": 0.0, "inner_gap_max": 0.0, "gap_basis": "right"}
    if len(cols) < 2 * min_cols_per_side:
        ev["verdict"] = "列簇不足（<%d×%d），非双栏" % (min_cols_per_side, min_cols_per_side)
        return None, ev
    # 间隙用**右缘**（与聚类口径一致；中心点会随数字位数漂移）
    edges = sorted(c["right"] for c in cols)
    gaps = [(edges[i + 1] - edges[i], i) for i in range(len(edges) - 1)]
    gmax, gi = max(gaps, key=lambda z: z[0])
    mid = (edges[gi] + edges[gi + 1]) / 2.0
    left = [c for c in cols if c["right"] < mid]
    right = [c for c in cols if c["right"] >= mid]
    ev.update({"max_gap": round(gmax, 4), "left": len(left), "right": len(right)})
    if len(left) < min_cols_per_side or len(right) < min_cols_per_side:
        ev["verdict"] = "切分后两侧列簇不足（%d|%d）⇒ 单栏" % (len(left), len(right))
        return None, ev
    inner = [g for g, i in gaps if i != gi]
    inner_max = max(inner) if inner else 0.0
    ev["inner_gap_max"] = round(inner_max, 4)
    if inner_max > 0 and gmax < inner_max * 1.2:
        ev["verdict"] = "组间间隙未显著大于组内最大间隙（%.4f vs %.4f）⇒ 单栏" % (
            gmax, inner_max)
        return None, ev
    ev["split_at"] = round(mid, 4)
    ev["verdict"] = "双栏：切分于 %.4f（左%d|右%d，组间间隙 %.4f > 组内最大 %.4f）" % (
        mid, len(left), len(right), gmax, inner_max)
    return mid, ev


def reconstruct(lines, clean_number, num_re=None):
    """几何重建一张页的表格。

    返回：{"ok","reason","columns","rows","panels","panel_count",
           "numeric_tokens","label_tokens","orphans"}
    """
    lines = [l for l in (lines or []) if l.get("text")]
    if not lines:
        return {"ok": False, "reason": "无 OCR 行", "columns": [], "rows": [], "panels": []}

    boxes = _boxes(lines)
    num_boxes = []
    label_boxes = []
    # ★ 排除**超高框**（页眉水印/竖排水印）：实测 A05-2 p6 的水印「仅供申万宏源汇福创投内部审阅使用」
    #   框高 0.1447 ≈ 10 倍中位框高（0.0146）⇒ 它的 y 区间会与**多行**数值重叠，
    #   在「重叠量最大」判据下霸占标签位（实测 4 行被贴成同一个水印标签）。
    #   ⇒ 标签框高度超过中位框高 2.5 倍 ⇒ 不是行标签（是水印/竖排文字）。
    _hs = sorted(b.get("h") or 0.014 for b in boxes)
    _med_h = _hs[len(_hs) // 2] if _hs else 0.014
    _max_label_h = _med_h * 2.5
    for b in boxes:
        v = _numeric_value(b["text"], clean_number)
        if v is not None:
            nb = dict(b); nb["v"] = v
            num_boxes.append(nb)
        else:
            t2 = _strip_footnote(b["text"])
            if len(CJK.findall(t2)) >= 2 and not _is_footnote_only(b["text"]) \
               and (b.get("h") or 0) <= _max_label_h:
                lb = dict(b); lb["text"] = t2
                label_boxes.append(lb)
    if len(num_boxes) < 2:
        return {"ok": False, "reason": "数值 token 不足（可能非表格页）",
                "columns": [], "rows": [], "panels": []}

    cols, pseudo = cluster_columns(num_boxes)
    if not cols:
        return {"ok": False, "reason": "无有效数值列", "columns": [], "rows": [], "panels": []}

    # ── 分栏（双栏表：左资产 / 右负债权益）──
    #   ★ 2026-11-15 判据重写。原来用「**标签 x 的最大间隙** > 0.18」判双栏，
    #     实测**两个方向都错**：
    #       · 漏判：<项目A> FDD p37 是双栏 BS（6 个数值列，左3|右3），
    #         但标签 x 间隙只有 0.1441 < 0.18 ⇒ 没分栏 ⇒ 左右栏混在一张表里
    #         ⇒ 资产总计与负债合计对不上（勾稽全毁）。
    #       · 误判：FDD 股权结构表首列是**序号**（孤立在 x≈0.17），
    #         其与右侧数值列的间隙高达 0.47~0.50，**比真双栏的 0.44 还大**
    #         ⇒ 用间隙判双栏会把单栏表误劈成两栏。
    #   ⇒ 改用「**有效数值列簇**」判据：按右缘间隙聚出列簇（已剔除伪列），
    #     在**最大间隙**处切分，要求**两侧各有 ≥2 个列簇**。
    #     实测 5 个样本全部判对：
    #       <项目A> p37（双栏）左3|右3 ✅｜FDD p37 左3|右1 ✗单栏 ✅
    #       FDD p20 左1|右3 ✗单栏 ✅｜FDD p12 左1|右2 ✗单栏 ✅｜审计 p6 左1|右1 ✗单栏 ✅
    split_at, panel_ev = detect_panels(cols)
    if split_at is None:
        panels = [{"cols": cols, "side": 0}]
    else:
        mid = split_at
        panels = [{"cols": [c for c in cols if c["center"] < mid], "side": 0},
                  {"cols": [c for c in cols if c["center"] >= mid], "side": 1}]

    orphans = list(pseudo)
    merged_rows = []          # 逻辑行合并噪声（相邻两行被并到一行）
    all_rows = []
    panel_infos = []
    for pi, p in enumerate(panels):
        pcols = p["cols"]
        # 本栏的标签（x 在本栏范围内，且在首个数值行之上 ⇒ 表头；之下 ⇒ 行标签）
        pnums = [t for c in pcols for t in c["tokens"]]
        first_val_y = min(t["y0"] for t in pnums) if pnums else 1.0
        panel_labels = []
        for b in label_boxes:
            if split_at is None:
                side_ok = True
            else:
                side_ok = (b["cx"] < split_at) == (p["side"] == 0)
            if not side_ok:
                continue
            # 标签必须在本栏数值块的**水平跨度内**（左于最后一个数值列）。
            #   ★ 2026-11-15 修正：原判据是「左于**第一个**数值列」，那假设了
            #     **标签列是首列**。但 FDD 股权结构表首列是**序号**（1、2、3…）：
            #       | 1 | 黄早红 | 3,044.5000 | 34.38% |
            #     ⇒ 标签（黄早红）落在第 2 列，被原判据整列排除 ⇒ **整行丢失**
            #       （实测 p20：几何重建返回 0 行，而该页实际有 24 行）。
            #   ⇒ 改为「左于最后一个数值列」：既容得下序号后的标签，
            #     又不会把数值块右侧的注释/脚注当成标签。
            if b["x0"] >= pcols[-1]["left"] - 0.02:
                continue
            panel_labels.append(b)
        # 逻辑行
        groups = _group_numeric_rows(pcols)
        rows_out = []
        used_groups = []          # 已占用的数值行（用于区分「上一行的标签」与「本行的标签」）
        for grp in groups:
            cells = [None] * len(pcols)
            for j, t in grp["members"]:
                if cells[j] is None:
                    cells[j] = t["v"]
                else:
                    # 同一格有第二个数 ⇒ **逻辑行聚类把相邻两行合并了**
                    #   （容差偏大）。这不是「丢数」（第一个值已保留），
                    #   而是「行被合并」的信号 → 记为**行合并**噪声，不算孤儿值。
                    merged_rows.append({"text": t["text"], "v": t["v"],
                                        "x": round(t["cx"], 4), "y": round(t["y0"], 4),
                                        "reason": "同一格重复取值（逻辑行合并）"})
            if not any(v is not None for v in cells):
                continue
            # 归标签：**纵向区间重叠**判据（不是「最近」！）
            #   实测 A05-2 p6：「货币资金」标签 y=0.1588~0.1744，
            #   同一行数值 142,338,049.42 y=0.1658~0.1804 ⇒ **重叠 0.0086**。
            #   下一行的标签（结算备付金 y=0.1739~）与本组数值**也**重叠一点，
            #   但重叠量更小 ⇒ 取**重叠量最大**者，而不是「中心差最小」。
            gy0 = min(t["y0"] for _j, t in grp["members"])
            gy1 = max(t["y1"] for _j, t in grp["members"])
            best = None
            for b in panel_labels:
                ov = min(b["y1"], gy1) - max(b["y0"], gy0)
                if ov <= 0:
                    continue
                # ★ 排除「**下一行**」的标签：它的顶边晚于本行数值的顶边。
                #   （实测 A05-2 p6 第 1 行：候选有两个，x 几乎相同——
                #     「结算备付金」x0=0.1854、y0=0.1739（**下一行**的标签，OCR 误识别）
                #     「货币资金」  x0=0.1861、y0=0.1588（本行标签）
                #     只按 x 取最左会选错 ⇒ 必须叠加「标签顶 ≤ 数值顶」。)
                if b["y0"] > gy0 + 0.004:
                    continue
                # 同一行的多个候选里取**最左**的：
                #   序号列是数字（本就不在 label_boxes 里）；
                #   表中间的说明列（如「货币」「持股比例」）更靠右 ⇒ 不会抢走行标签。
                #   （实测 FDD p12：候选 [黄早红(x=0.13), 货币(x=0.60)] ⇒ 取黄早红。）
                if best is None or b["x0"] < best[1]["x0"]:
                    best = (ov, b)
            if best is not None:
                lb = best[1]
            else:
                # 向上最近：**从本行往上**找第一个「本栏尚无值」的标签行。
                #   （实测 A05-3 p11：「营业收入」标签区间与其数值区间**不重叠**
                #     —— OCR 把标签放在更高的 y 带；「减：营业成本」却在下一行。
                #     哪个是真标签？⇒ 靠「本栏已占用的行」来区分：
                #     真正的行标签是**本行自己的**，而下一行的标签已被下一行的数占用。
                #     故取「向上第一个、且其下方到本组之间没有其他已占用的数值行」的标签。）
                # 向上找标签：取**上方最近**、且**其下方到本组之间没有别的数值行**的标签。
                #   （实测 A05-3 p11 母公司利润表：「营业收入」标签与本组数值**不重叠**
                #     ——OCR 把标签放进了更高的 y 带；「减：营业成本」在下一行且与本组重叠更多，
                #     故重叠判据会选错。此时靠「中间无别的数值行」区分：
                #     真正的行标签下方不该再夹着另一个数值行。）
                above = [b for b in panel_labels if b["y1"] <= gy0 + 0.002]
                lb = None
                if above:
                    above.sort(key=lambda b: b["y0"], reverse=True)
                    for b in above:
                        between = [g2 for g2 in used_groups
                                   if g2["y0"] < gy0 - 0.002 and g2["y1"] > b["y1"] + 0.001]
                        if between:
                            continue          # 中间夹着别的数值行 ⇒ 不是本组的标签
                        lb = b
                        break
                if lb is None:
                    above2 = [b for b in panel_labels if b["y1"] <= gy0 + 0.002]
                    lb = max(above2, key=lambda b: b["y0"]) if above2 else None
                if lb is None:
                    orphans.append({"text": "|".join(t["text"] for _j, t in grp["members"])[:40],
                                    "v": None, "x": round(grp["members"][0][1]["cx"], 4),
                                    "y": round(gy0, 4), "reason": "数值行无科目名"})
                    continue
            used_groups.append({"y0": gy0, "y1": gy1})
            rows_out.append({"label": lb["text"], "cells": cells, "y": gy0,
                             "x": lb["x0"], "panel": pi,
                             "has_value": True})
        # 列名（表头）：首个数值行之上、x 与该列重叠
        names = []
        for c in pcols:
            cands = [b for b in panel_labels
                     if b["y0"] < first_val_y
                     and not (b["x1"] < c["left"] - 0.02 or b["x0"] > c["right"] + 0.02)]
            best = max(cands, key=lambda b: b["y0"]) if cands else None
            names.append(best["text"] if best else None)
        panel_infos.append({"panel": pi, "columns": len(pcols),
                            "x_range": [round(pcols[0]["left"], 4), round(pcols[-1]["right"], 4)],
                            "header": names, "rows": rows_out})
        all_rows.extend(rows_out)

    cols_out = []
    for pi, p in enumerate(panels):
        names = (panel_infos[pi].get("header") or []) if pi < len(panel_infos) else []
        for ci, c in enumerate(p["cols"]):
            cols_out.append({"panel": pi, "col": ci, "left": round(c["left"], 4),
                             "right": round(c["right"], 4),
                             "center": round(c["center"], 4),
                             "name": names[ci] if ci < len(names) else None})

    return {"ok": True, "reason": None, "columns": cols_out, "rows": all_rows,
            "panels": panel_infos, "panel_count": len(panels),
            "panel_evidence": panel_ev,
            "numeric_tokens": len(num_boxes), "label_tokens": len(label_boxes),
            "orphans": orphans, "merged_rows": merged_rows}
