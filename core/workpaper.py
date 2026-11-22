#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-ocr · 审计/底稿索引生成器（workpaper index）

把 doc_ocr / doc_preprocess 的 JSON 产物，转成「报告底稿一对一」的带地址索引：
每个证据片段（文本页 / 表格行 / 条款命中）都有稳定地址，回答必须引用地址，
找不到证据就标 missing 待查 —— 这是审计底稿 RAG 的纪律（参考 vestigium 的
页地址协议与 financial-report-rag 的表格行级 chunk）。

地址协议（address）：
  <doc_id>:P<page>            文本页（doc_ocr text 模式 / pipeline 逐页文本）
  <doc_id>:P<page>T<t>R<r>    扫描件表格行（doc_ocr table 模式，每页一个表）
  <doc_id>:T<t>R<r>           Office 表格行（xlsx/docx/csv，无页码概念）
  <doc_id>:P<page>C<c>        条款命中（clause 模式，c 为该类条款第 n 处）

  doc_id = 源文件 sha256 前 8 位（源文件可寻址时），否则用文件名。

输入：目录或单个 JSON（识别两类 schema）
  · pipeline（doc_preprocess）: {source, sha256, kind, tables:[[...]], ...}
  · engine（doc_ocr）          : {input, pages:[{page,text,table?}], clauses?}

输出（--output 目录）：
  workpaper_index.jsonl  每行一条证据（RAG chunk 源：BM25/向量都可直接挂）
  workpaper_index.md     可读索引（按文档分组，表格带地址列）

纪律（写进每个产物头部）：
  1. 证据链：每条结论必须引用地址；地址在索引里必须能反查回原文与 sha256。
  2. 不编造：索引里没有的证据，回答只能标 missing 待查，不得补数。
  3. 计算层与阅读层分离：JSONL 是计算层（给 RAG/程序），MD 是阅读层（给人）。

用法：
  python3 core/workpaper.py --input <目录或json> --output <目录>
"""
import argparse
import hashlib
import json
import os
import re
import sys


# ---------------------------------------------------------------------------
# 输入发现与 schema 识别
# ---------------------------------------------------------------------------
def discover_jsons(input_path):
    if os.path.isfile(input_path):
        return [input_path]
    out = []
    for root, dirs, files in os.walk(input_path):
        for f in sorted(files):
            if f.endswith(".json") and f != "cache.json":
                out.append(os.path.join(root, f))
    return out


def sha8_of(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:8]
    except Exception:
        return None


def load_records(json_paths):
    """识别两类 schema，统一成 {doc_id, doc_name, source_path, source_sha256, kind, pages, tables, clauses}。"""
    records = []
    for jp in json_paths:
        try:
            d = json.load(open(jp, encoding="utf-8"))
        except Exception:
            continue
        rec = {"pages": [], "tables": [], "clauses": [], "source_path": None,
               "source_sha256": None, "kind": None, "doc_name": None}
        if isinstance(d, dict) and "pages" in d:
            # engine（doc_ocr）schema
            src = d.get("input")
            rec["source_path"] = src
            rec["source_sha256"] = sha8_of(src) if src and os.path.exists(src) else None
            rec["doc_name"] = os.path.basename(src) if src else os.path.basename(jp)
            rec["kind"] = d.get("mode")
            rec["pages"] = d.get("pages") or []
            rec["clauses"] = d.get("clauses") or []
        elif isinstance(d, dict) and ("tables" in d or "source" in d):
            # pipeline（doc_preprocess）schema
            src = d.get("source")
            rec["source_path"] = src
            sha = d.get("sha256")
            rec["source_sha256"] = sha[:8] if isinstance(sha, str) and len(sha) >= 8 else (
                sha8_of(src) if src and os.path.exists(src) else None)
            rec["doc_name"] = os.path.basename(src) if src else os.path.basename(jp)
            rec["kind"] = d.get("kind")
            rec["tables"] = d.get("tables") or []
        else:
            continue
        rec["doc_id"] = rec["source_sha256"] or re.sub(r"[^\w\-]", "_", rec["doc_name"])[:12]
        records.append(rec)

    # 同一源文档的多轮扫描（如 clause + legal）合并为一个记录，保证地址唯一：
    # 页取首个非空、条款按类型去重（首见优先）、表序号续排。
    merged = {}
    order = []
    for rec in records:
        did = rec["doc_id"]
        if did not in merged:
            merged[did] = rec
            order.append(did)
            continue
        m = merged[did]
        seen_pages = {p.get("page") for p in m["pages"] if p.get("page")}
        for p in rec["pages"]:
            if p.get("page") not in seen_pages:
                m["pages"].append(p)
        exist_types = {c.get("clause_type") for c in m["clauses"]}
        for c in rec["clauses"]:
            if c.get("clause_type") not in exist_types:
                m["clauses"].append(c)
        offset = len(m["tables"])
        for t in rec["tables"]:
            if isinstance(t, list):
                m["tables"].append(t)
        _ = offset  # T 序号由枚举时的 ti 生成；同 doc 多 pipeline 表按顺序续排
        m["scan_rounds"] = m.get("scan_rounds", 1) + 1
    return [merged[did] for did in order]


# ---------------------------------------------------------------------------
# 展开为带地址的证据条目
# ---------------------------------------------------------------------------
def _clean_cell(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    if s == "":
        return None
    return s


def entries_for_record(rec):
    """把一个文档记录展开为证据条目列表。"""
    E = []
    doc_id = rec["doc_id"]

    # 文本页（engine schema）
    for p in rec["pages"]:
        page = p.get("page")
        text = (p.get("text") or "").strip()
        if page and text:
            E.append({"address": "%s:P%s" % (doc_id, page), "kind": "text_page",
                      "doc": rec["doc_name"], "page": page,
                      "chars": len(text), "text": text,
                      "source_path": rec["source_path"]})
        # 扫描件表格行（doc_ocr table 模式：每页一个 table）
        t = p.get("table") or {}
        rows = t.get("rows") or []
        for ri, r in enumerate(rows, 1):
            cells = [_clean_cell(c) for c in r.get("cells", [])]
            E.append({"address": "%s:P%sT1R%d" % (doc_id, page, ri),
                      "kind": "table_row", "doc": rec["doc_name"], "page": page,
                      "table": 1, "row": ri, "label": (r.get("label") or "").strip(),
                      "values": cells, "conf": r.get("conf"),
                      "source_path": rec["source_path"]})

    # Office 表格（pipeline schema：tables[t] 是行数组，无页码）
    for ti, tbl in enumerate(rec["tables"], 1):
        if not isinstance(tbl, list):
            continue
        for ri, row in enumerate(tbl, 1):
            if not any(str(c).strip() for c in row if c is not None):
                continue
            cells = [_clean_cell(c) for c in row]
            E.append({"address": "%s:T%dR%d" % (doc_id, ti, ri),
                      "kind": "table_row", "doc": rec["doc_name"], "table": ti,
                      "row": ri, "label": (cells[0] if cells else "") or "",
                      "values": cells[1:] if len(cells) > 1 else [],
                      "source_path": rec["source_path"]})

    # 条款命中（clause 模式）
    # 条款/扫描命中（clause/legal/strategy）：按类型排序分配 C 序号，保证地址唯一
    # （此前按「引用序号」编号，同页多类型会撞地址）
    for ci, c in enumerate(sorted(rec["clauses"], key=lambda c: c.get("clause_type") or ""), 1):
        # ★ 2026-07-20 加**假地址守卫**：`clause_hits` 曾把**零命中**的关键词类型
        #   也登记成条目（`quotes=[]`、`first_page=None`）⇒ 这里 `first_page or 1`
        #   会把它编成 `<doc_id>:P1C<n>`：**指向第 1 页、原文并无该条款的假地址**。
        #   （2026-11-20 实测：P1 批 98 条里 51 条是这种假条目）
        # ⇒ 零命中 / 无页码 / 无引文的条目**不编地址**（宁缺勿造）。
        #   上游 `clause_hits` 已同步修正（只在真的匹配上时才登记类型）。
        page = c.get("first_page")
        quotes = c.get("quotes") or []
        if not page or not quotes:
            continue
        q = quotes[0] if quotes else ""
        E.append({"address": "%s:P%sC%d" % (doc_id, page, ci),
                  "kind": "clause_hit", "doc": rec["doc_name"],
                  "page": page,
                  "clause_type": c.get("clause_type"),
                  "quote": (q or "")[:220],
                  "source_path": rec["source_path"]})
    return E


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------
DISCIPLINE = (
    "证据链纪律：每条结论必须引用 address；address 可反查 source_path + doc_id（sha256 前 8 位）。\n"
    "不编造：本索引里没有的证据，回答只能标 missing 待查，不得补数。\n"
    "双层：JSONL 是计算层（RAG chunk 源），MD 是阅读层（人工复核）。"
)


def write_outputs(entries, records, out_dir, title):
    os.makedirs(out_dir, exist_ok=True)
    jsonl_path = os.path.join(out_dir, "workpaper_index.jsonl")
    md_path = os.path.join(out_dir, "workpaper_index.md")

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # 跨页 BS 勾稽（场景 B：审计报告 BS 常跨页，单页 balance_check 常为 0/0）
    # 聚合同一文档全部 table_row 再跑一次 balance_check，结果写进 MD 头部。
    from ocr import balance_check
    cross_checks = []
    for rec in records:
        rows = [{"label": e.get("label") or "", "cells": e.get("values") or []}
                for e in entries if e["doc"] == rec["doc_name"] and e["kind"] == "table_row"]
        bcs = balance_check(rows)
        if bcs:
            cross_checks.append({"doc": rec["doc_name"], "doc_id": rec["doc_id"],
                                 "balance_checks": bcs,
                                 "balanced": all(b["balanced"] for b in bcs)})

    L = ["# %s\n" % title,
         "- 文档数：%d · 证据条目：%d" % (len(records), len(entries)),
         "",
         "```", DISCIPLINE, "```", ""]
    if cross_checks:
        L.append("\n## 跨页 BS 勾稽（聚合同文档全部表格行）\n")
        for cc in cross_checks:
            mark = "✓" if cc["balanced"] else "✗"
            L.append("- %s `%s`：%s" % (mark, cc["doc_id"], cc["doc"]))
            for b in cc["balance_checks"]:
                L.append("  - col%d：资产总计 %s vs 负债+权益 %s（diff=%s，%s）" % (
                    b["col"], b["assets_total"], b["liab_plus_equity"], b["diff"],
                    "平衡" if b["balanced"] else "不平"))

    by_doc = {}
    for e in entries:
        by_doc.setdefault(e["doc"], []).append(e)

    for rec in records:
        name = rec["doc_name"]
        es = by_doc.get(name, [])
        L.append("\n## %s\n" % name)
        L.append("- doc_id：`%s` · 来源：%s" % (rec["doc_id"], rec["source_path"] or "(未知)"))
        if not es:
            L.append("- （无可索引证据）")
            continue

        # 条款命中
        hits = [e for e in es if e["kind"] == "clause_hit"]
        if hits:
            L.append("\n### 条款命中\n")
            L.append("| 地址 | 类型 | 页 | 引用 |")
            L.append("|---|---|---|---|")
            for e in hits:
                L.append("| `%s` | %s | %s | %s |" % (
                    e["address"], e.get("clause_type") or "", e.get("page") or "",
                    (e.get("quote") or "")[:80]))

        # 表格行
        rows = [e for e in es if e["kind"] == "table_row"]
        if rows:
            tables = {}
            for e in rows:
                tables.setdefault(e.get("table") or 1, []).append(e)
            for tno, tes in sorted(tables.items()):
                ncol = max(len(e.get("values") or []) for e in tes) or 1
                pno = tes[0].get("page")
                L.append("\n### 表 T%d%s（%d 行）\n" % (tno, " · P%s" % pno if pno else "", len(tes)))
                header = "| 地址 | 项目 | " + " | ".join("列%d" % i for i in range(ncol)) + " |"
                L.append(header)
                L.append("|---|---|" + "---|" * ncol)
                for e in tes:
                    vals = e.get("values") or []
                    vals = vals + [""] * (ncol - len(vals))
                    cells = [("" if v is None else _fmt(v)) for v in vals]
                    L.append("| `%s` | %s | %s |" % (e["address"], e.get("label") or "", " | ".join(cells)))

        # 文本页
        pages = [e for e in es if e["kind"] == "text_page"]
        if pages:
            L.append("\n### 文本页（%d 页，正文见 JSONL）\n" % len(pages))
            L.append("| 地址 | 字符数 | 开头 |")
            L.append("|---|---|---|")
            for e in pages:
                L.append("| `%s` | %s | %s |" % (
                    e["address"], e.get("chars") or 0,
                    (e.get("text") or "").replace("\n", " ")[:60]))

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return jsonl_path, md_path


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float) and abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="审计/底稿索引生成器（带地址证据）")
    ap.add_argument("--input", required=True, help="JSON 文件或目录（engine / pipeline 产物）")
    ap.add_argument("--output", required=True, help="索引输出目录")
    ap.add_argument("--title", default="底稿索引（workpaper index）")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print(json.dumps({"ok": False, "error": "input not found: %s" % args.input}, ensure_ascii=False))
        return

    paths = discover_jsons(args.input)
    records = load_records(paths)
    entries = []
    for rec in records:
        entries.extend(entries_for_record(rec))

    jsonl_path, md_path = write_outputs(entries, records, os.path.abspath(args.output), args.title)
    out = {
        "ok": True,
        "input": os.path.abspath(args.input),
        "documents": len(records),
        "entries": len(entries),
        "by_kind": {},
        "output": {"jsonl": jsonl_path, "md": md_path},
        "discipline": DISCIPLINE,
    }
    for e in entries:
        out["by_kind"][e["kind"]] = out["by_kind"].get(e["kind"], 0) + 1
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
