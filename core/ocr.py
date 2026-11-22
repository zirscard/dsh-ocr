#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-ocr · 可复用 OCR 引擎（扫描件 → 文本 / 表格 / 条款）

定位：一个可以被 DSH 插件（dsh-ocr）反复调用的 OCR 引擎。输入一份扫描 PDF 或图片，
按三种模式产出结构化结果：
  · text   整页 OCR：逐页文本 + 行数 + 平均置信度（最通用）。
  · table  财务报表：整页 OCR 定位数值列 → 列切条重 OCR → 按 y 归行 → 数字清洗
           → 轻量勾稽自校验（解决"无竖线三线表整页 OCR 漏识 ~1/3 数字"的问题）。
  · clause 合同条款：关键词正则抽取（回购/对赌/反稀释/优先清算…）+ 页码 + 引用。

引擎选择：RapidOCR（onnxruntime 跑 PaddleOCR PP-OCRv4 的 ONNX 模型）。
  它带 C 扩展（onnxruntime / cv2 / pypdfium2），在 DSH 内置 python 下会被 macOS
  hardened runtime 的 library validation 拒绝加载；必须用能加载这些 .so 的解释器
  （Anaconda，或任何设了 DSH_OCR_PYTHON 的解释器）。runner.js 负责选对解释器，
  本脚本只负责干活。

参考：若 RapidOCR 整页文本仍不够（复杂表格结构、多栏版式、表格单元格 span），
升级路径是 PaddleOCR 的文档解析流水线 paddleocr-doc-parsing（PP-Structure：版面
分析 + 表格结构识别）。本脚本的 table 模式是其"表格结构识别"的一版轻量实现
（x 聚类定列 + 列切条），详见 README「文档解析参考」。

用法：
  python ocr.py --input x.pdf --mode text --pages all --dpi 200
  python ocr.py --input x.pdf --mode table --pages 34,37,42 --dpi 400
  python ocr.py --input x.png --mode clause
产物：JSON 打到 stdout；--out-json / --out-md 落盘（可选）。
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# 数字清洗（逗号/句点混淆、千分位、括号负数）—— 移植自数据底表 FDD 抽取
# ---------------------------------------------------------------------------
NUM_RE = re.compile(r"[（(]?\s*[-\u2212]?\s*\d[\d,，.．]*\d?\s*[)）]?")


def clean_number(tok):
    """OCR 数字 → float。处理：全角/半角括号负号、千分位逗号、逗号句点混淆、尾部句点。

    OCR 常见混淆：千分位逗号被认成句点（52,163 → 52.163）、逗号句点双混
    （2,032.898 → 2.032.898）。按"千位分隔"规则归一，失败返回 None（不硬凑）。
    """
    t = (tok or "").strip().replace("（", "(").replace("）", ")").replace("，", ",").replace("．", ".")
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1].strip()
    if t.startswith(("-", "\u2212")):
        neg, t = True, t[1:].strip()
    t = t.replace(" ", "").replace(",", "")
    if t.endswith("."):
        t = t[:-1]
    if re.fullmatch(r"\d+\.\d{3}", t):            # 千分位逗号被认成句点
        t = t.replace(".", "")
    elif re.fullmatch(r"\d+\.\d{3},\d+", t):
        t = t.replace(".", "").replace(",", "")
    if t.count(".") > 1:
        head, _, tail = t.rpartition(".")
        t = head.replace(".", "") + "." + tail
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def numbers_in(text):
    return [v for v in (clean_number(m.group(0)) for m in NUM_RE.finditer(text or "")) if v is not None]


# ---------------------------------------------------------------------------
# 输入渲染（PDF → PIL 图 / 图片直读）
# ---------------------------------------------------------------------------
IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


def resolve_pages(spec, total):
    """把 --pages 解析成 1-based 页号列表。支持 all / '1-10' / '3,7' / '1-3,9'。"""
    if spec is None or str(spec).strip().lower() in ("", "all"):
        return list(range(1, total + 1))
    out, seen = [], set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            for p in range(min(a, b), max(a, b) + 1):
                if 1 <= p <= total and p not in seen:
                    seen.add(p)
                    out.append(p)
        else:
            p = int(part)
            if 1 <= p <= total and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def load_images(path, pages, dpi):
    """返回 [(page_no, PIL.Image, meta)]。PDF 用 pypdfium2 渲染，图片直读。"""
    ext = os.path.splitext(path)[1].lower()
    imgs = []
    if ext in IMG_EXTS:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        imgs.append((1, im, {"type": "image", "w": im.width, "h": im.height}))
        return imgs
    # PDF
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path)
    total = len(pdf)
    if not pages:
        pages = list(range(1, total + 1))
    for pno in pages:
        img = pdf[pno - 1].render(scale=dpi / 72.0).to_pil().convert("RGB")
        imgs.append((pno, img, {"type": "pdf", "page": pno, "total": total}))
    pdf.close()
    return imgs


# ---------------------------------------------------------------------------
# OCR 引擎
# ---------------------------------------------------------------------------
def _rapidocr_version():
    try:
        import importlib.metadata as md
        return md.version("rapidocr")
    except Exception:
        return "?"


def _ocr_model_version():
    """当前 OCR 权重版本：DSH_OCR_MODEL=v4|v5|v6。

    默认 v6（PP-OCRv6，50 语种统一模型）——2026-10-28 验收：
      · <项目A> FDD p37 真值回归 15/15，BS 勾稽 3/3（608209/486041/831229 与 M1 底稿一致）；
      · 星环审计报告 p6 标签碎片 0（原为 '营/采/减：'），资产总计读数正确；
      · v4 保留为回滚档（DSH_OCR_MODEL=v4），两者在真值集上结果一致。
    """
    v = (os.environ.get("DSH_OCR_MODEL") or "v6").strip().lower()
    return v if v in ("v4", "v5", "v6") else "v6"


def load_engine(lang, version=None):
    """懒加载 RapidOCR。失败给结构化错误 + 修复提示（而不是 traceback）。

    并发关键：RapidOCR/onnxruntime 默认 intra_op_num_threads=-1（用满 CPU 核数）。
    每次 doc_ocr 都 spawn 一个独立 Python 进程，N 个并发就是 N×核数 线程，CPU 严重
    超卖、单进程被拖到看起来「卡住」。因此线程数按**并发度自适应**：

      · 默认 6 线程/进程（2026-10-29 实测：M2 Pro 12 核，PP-OCRv6 单页 A4@300dpi
        intra=1 →11.67s、2→6.37s、**6→2.84s**（最快）、8→3.39s、12→6.49s；
        6 线程比原默认 2 线程快 **2.2×**，输出完全一致（297 行、conf 0.9940））
      · 但并发跑时（DSH_OCR_MAX_CONCURRENT≥2）回到 2 线程/进程，避免 N×6 超卖
      · DSH_OCR_THREADS 可显式覆盖；设 0 或负数表示不限/走默认

    CoreML EP 实测**更慢**（7.56s vs CPU 4.12s）：PP-OCRv6 的动态 shape 图无法
    静态绑定到 ANE，CoreML 每次回落到 CPU 还要付转换开销。故默认保持 CPU EP。

    版本切换（2026-10-28，零成本换血）：
      · v4 → rapidocr_onnxruntime 1.4.4（PP-OCRv4 ONNX，已验证口径）
      · v5/v6 → rapidocr 3.10（PP-OCRv5 / PP-OCRv6 ONNX，v6 为 50 语种统一模型）
      · DSH_OCR_MODEL=v4|v5|v6 控制；v5/v6 权重缺失时自动回退 v4 并在结果中标注。
    """
    version = (version or _ocr_model_version()).strip().lower()
    env_threads = os.environ.get("DSH_OCR_THREADS", "").strip()
    if env_threads:
        try:
            n = int(env_threads)
        except ValueError:
            n = 2
    else:
        # 自适应：单进程 6 线程最快（M2 Pro 实测 2.84s vs 2 线程 6.37s）；
        # 并发时回到 2 线程/进程防 CPU 超卖。
        # 判据用**本次实际并发度**（runner 用 DSH_OCR_CONCURRENCY 传当前在跑的任务数），
        # 不能用 DSH_OCR_MAX_CONCURRENT（那是"允许的上限"，不是"当前有几个在跑"）。
        try:
            conc = int(os.environ.get("DSH_OCR_CONCURRENCY", "1").strip())
        except ValueError:
            conc = 1
        n = 2 if conc >= 2 else 6
    if n <= 0:
        n = -1

    if version in ("v5", "v6"):
        try:
            from rapidocr import RapidOCR, OCRVersion
            ver = OCRVersion.PPOCRV6 if version == "v6" else OCRVersion.PPOCRV5
            params = {
                "Global.text_score": 0.5,
                "EngineConfig.onnxruntime.intra_op_num_threads": n,
                # inter_op 固定 1：实测 intra=6 + inter=6 反而变慢（6.27s vs 2.84s），
                # 推理是单 session 串行图，并行度只需 intra。
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "Global.log_level": "error",
            }
            # ── 权重档位（2026-11-14 新增）：tiny / small / medium
            #
            # 为什么需要：PP-OCRv6 有三档权重（tiny 1.5M / small 7.7M / medium 34.5M），
            # 此前只装了 small，档位不可选。medium 实测更准但慢约 4 倍，故**默认仍是
            # small**（保持现网性能基线），按需用 DSH_OCR_TIER=medium 显式开启。
            #
            # 真值对比（2026-11-14，m3/ocr/pages 6 页，同一批图）：
            #   small 均分 0.9860 → medium 0.9916（+0.0056）；better 5 / worse 1 / same 0
            #   典型改进：半角括号 `(一)` → 全角 `（一）`（中文法律文书的关键差异）
            #   代价：单页 0.99s → 3.79s（3.83×）
            # 建议：条款/合同/盖章件等字符级敏感页用 medium；大批量财报页仍用 small
            #      （财报靠勾稽自校验兜数字，不值得 4 倍耗时）。
            tier = (os.environ.get("DSH_OCR_TIER") or "small").strip().lower()
            if tier in ("tiny", "small", "medium") and version == "v6":
                _md = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "_deps", "rapidocr", "models")
                _det = os.path.join(_md, "PP-OCRv6_det_%s.onnx" % tier)
                _rec = os.path.join(_md, "PP-OCRv6_rec_%s.onnx" % tier)
                if os.path.isfile(_det) and os.path.isfile(_rec):
                    params["Det.model_path"] = _det
                    params["Rec.model_path"] = _rec
                    tier_used = tier
                else:
                    tier_used = "small(权重缺失回落)"
            elif version == "v6":
                tier_used = "small(未知档位回落)"
            else:
                tier_used = "n/a(v%s 固定档)" % version[1:]
            engine = RapidOCR(params=params)
            engine._dsh_model = version
            engine._dsh_tier = tier_used
            engine._dsh_engine = "rapidocr %s (PP-OCRv%s ONNX · %s)" % (
                _rapidocr_version(), version[1:], tier_used)
            return engine
        except Exception as e:  # 权重缺失/依赖异常 → 结构化回退
            raise RuntimeError(
                "OCR 引擎 %s 不可用（%s）。\n"
                "处置：DSH_OCR_MODEL=v4 回退到已验证的 PP-OCRv4；"
                "或补装权重：pip install -U rapidocr（v5 首次运行需从 modelscope 下载）。" % (version, e))

    try:
        from rapidocr_onnxruntime import RapidOCR
        kwargs = {}
        if lang and lang.lower() in ("en", "eng"):
            kwargs["text_score"] = 0.5
        if n > 0:
            kwargs["intra_op_num_threads"] = n
            kwargs["inter_op_num_threads"] = n
        engine = RapidOCR(**kwargs)
        engine._dsh_model = "v4"
        engine._dsh_engine = "rapidocr-onnxruntime (PP-OCRv4 ONNX)"
        return engine
    except ImportError as e:
        raise RuntimeError(
            "OCR 引擎不可用：rapidocr_onnxruntime 未安装或 C 扩展无法加载（%s）。\n"
            "修复：用能加载 onnxruntime/cv2 的解释器运行（设 DSH_OCR_PYTHON 指向 Anaconda，"
            "或 `pip install rapidocr-onnxruntime` 到该解释器）。" % e)


def run_page(engine, img):
    """对一张 PIL 图跑 RapidOCR，返回行列表 [{x,y,w,h,text,conf}]（x/y 为左上，归一化到 0..1）。

    兼容两种引擎返回：
      · v4（rapidocr_onnxruntime 1.4.4）→ (res, elapse)，res = [[box, text, score], ...]
      · v5/v6（rapidocr 3.10）→ RapidOCROutput(txts, scores, boxes)
    """
    out = engine(img)
    if isinstance(out, tuple):
        res = out[0]
        rows = [(r[0], r[1], r[2]) for r in (res or [])]
    else:
        txts = getattr(out, "txts", None)
        scores = getattr(out, "scores", None)
        boxes = getattr(out, "boxes", None)
        txts = [] if txts is None else list(txts)
        scores = [] if scores is None else list(scores)
        boxes = [] if boxes is None else list(boxes)
        rows = [(boxes[i], txts[i], scores[i]) for i in range(len(txts))]
    W, H = img.width, img.height
    lines = []
    if not rows:
        return lines
    for box, text, score in rows:
        if box is None:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        lines.append({
            "x": round(x0 / W, 4), "y": round(y0 / H, 4),
            "w": round((x1 - x0) / W, 4), "h": round((y1 - y0) / H, 4),
            "text": str(text), "conf": round(float(score), 4),
        })
    return lines


def page_text(lines):
    return "\n".join(l["text"] for l in sorted(lines, key=lambda l: (l["y"], l["x"])))


# ---------------------------------------------------------------------------
# 模式：text / clause / legal / strategy / table
# ---------------------------------------------------------------------------
# 交易/投资条款（原 clause 模式口径）
CLAUSE_PATTERNS = {
    "回购": r"回购",
    "对赌/业绩承诺": r"对赌|业绩承诺|业绩补偿|估值调整",
    "反稀释": r"反稀释",
    "优先清算": r"优先清算|清算优先",
    "上市/退出": r"首次公开发行|上市|合格IPO",
    "股权转让限制": r"股权转让|优先购买权",
    "一票否决/特别表决": r"一票否决|特别表决|一致同意",
}

# 法律尽调扫描（legal 模式）：主体/程序/责任/争议/期限/签署等尽调关注点
# 条款分类对齐 CUAD（Contract Understanding Atticus Dataset）41 类，按中文合同语境映射
LEGAL_PATTERNS = {
    "陈述与保证": r"陈述与保证|陈述保证|真实、准确|真实性和完整性",
    "违约责任": r"违约|赔偿|罚则|损害赔偿|违约金",
    "违约金/预定赔偿": r"违约金|预定损害赔偿|违约赔偿",
    "责任上限": r"责任上限|赔偿上限|累计责任|不超过.*赔偿",
    "争议解决": r"争议解决|仲裁|诉讼|管辖",
    "适用法律": r"适用法律|法律适用|中华人民共和国法律",
    "生效/终止/解除": r"生效|终止|解除|失效",
    "期限与窗口": r"期限|窗口|工作日内|日之内|之日起",
    "自动续约": r"自动续约|自动续期|顺延",
    "放弃与豁免": r"放弃|豁免|不可撤销",
    "保密": r"保密|商业秘密|保密信息",
    "竞业限制": r"竞业|不竞争|不得从事|竞争性业务",
    "排他性": r"排他|独家|唯一供应",
    "最惠国": r"最惠国|不低于.*条件|同等条件",
    "最低承诺": r"最低采购|最低订购|最低用量|保底",
    "控制权变更": r"控制权变更|实际控制人变更|控股权变更",
    "审计权": r"审计权|查阅权|核查权|检查权",
    "转让与权利限制": r"转让|优先购买权|优先认购|随售权|领售权|质押",
    "程序与审批": r"工商变更|减资程序|股东会|董事会决议|审批|备案|核准",
    "交叉引用": r"第[一二三四五六七八九十百\d]+条|详见|参见|附件",
    "签署与授权": r"盖章|签字|签署日期|授权代表|法定代表人",
    "合规与资质": r"合规|资质|许可证|牌照|处罚|诉讼",
    "保险义务": r"保险|投保|承保",
    "第三方受益人": r"第三方受益人|受益第三人",
    "禁止诋毁": r"诋毁|贬损|名誉",
    "源代码托管": r"源代码托管|代码托管|escrow",
    "不起诉承诺": r"不起诉|放弃诉讼|承诺不提起诉讼",
}

# 战略/商业报告扫描（strategy 模式）：市场/竞争/里程碑/风险等要素
STRATEGY_PATTERNS = {
    "市场规模": r"市场规模|TAM|SAM|SOM|市场空间|万亿",
    "增长与预测": r"增长率|CAGR|复合增长|同比增|预计|预测|展望",
    "竞争格局": r"市占率|市场份额|竞争格局|竞争对手|龙头|壁垒",
    "客户与订单": r"客户|订单|中标|框架协议|在手订单|前五大",
    "技术与研发": r"专利|知识产权|核心技术|研发投入|良率|技术路线",
    "里程碑与规划": r"里程碑|路线图|产能规划|战略规划|目标",
    "供应链": r"供应链|上游|下游|供应商|国产替代|原材料",
    "风险因素": r"风险|不确定性|瓶颈|挑战|波动",
    "政策与监管": r"政策|补贴|监管|准入|白名单",
    "财务亮点": r"营收|净利润|毛利率|在手现金|融资",
}

# 模式 → 关键词集注册表（clause/legal/strategy 共用同一命中与地址机制）
SCAN_PATTERNS = {
    "clause": CLAUSE_PATTERNS,
    "legal": LEGAL_PATTERNS,
    "strategy": STRATEGY_PATTERNS,
}


def clause_hits(pages_lines, patterns=None):
    """关键词扫描抽取：命中 → 聚合（类型 + 命中页数 + 首页 + 最多 3 条去重引用）。"""
    patterns = patterns or CLAUSE_PATTERNS
    by_type = {}
    for pno, lines in pages_lines:
        txt = "\n".join(l["text"] for l in lines)
        for name, pat in patterns.items():
            # ★ 2026-07-20 修**假命中**：原实现把 `by_type.setdefault(...)` 放在
            #   `for m in re.finditer(...)` **之外** ⇒ **每个关键词类型都被无条件登记**，
            #   哪怕一次都没匹配上 ⇒ 输出里恒有全部类型，且 `quotes=[]`、`first_page=None`。
            #   下游 `workpaper.py` 会把这种**零命中**条目照样编成地址
            #   （`<doc_id>:P1C<n>`，`first_page` 为 None ⇒ 回落到第 1 页）
            #   ⇒ **产出指向第 1 页、原文无对应条款的假地址**。
            #   （2026-11-20 实测：P1 批 98 条条款命中里 **51 条是零命中假条目**）
            # ⇒ 改为**只在真的匹配上时**才登记该类型（本改动只会**去掉**假命中，
            #   不会少掉任何真实命中）。
            for m in re.finditer(pat, txt):
                hits = by_type.setdefault(name, {"pages": set(), "quotes": []})
                s = max(0, m.start() - 90)
                quote = re.sub(r"\s+", " ", txt[s:m.start() + 130]).strip()[:220]
                hits["pages"].add(pno)
                if len(hits["quotes"]) < 3 and quote not in hits["quotes"]:
                    hits["quotes"].append(quote)
    clauses = []
    for name, h in by_type.items():
        pages = sorted(h["pages"])
        clauses.append({
            "clause_type": name,
            "page_count_hit": len(pages),
            "first_page": pages[0] if pages else None,
            "quotes": h["quotes"],
        })
    clauses.sort(key=lambda c: c["clause_type"])
    return clauses


# --- table：数值列检测（1-D k-means） --------------------------------------
def kmeans1d(xs, k=3, iters=60):
    xs = sorted(xs)
    if len(xs) < k:
        k = max(1, len(xs))
    centers = [xs[int(len(xs) * (i + 0.5) / k)] for i in range(k)]
    for _ in range(iters):
        buckets = [[] for _ in range(k)]
        for x in xs:
            j = min(range(k), key=lambda t: abs(x - centers[t]))
            buckets[j].append(x)
        new = [sum(b) / len(b) if b else centers[j] for j, b in enumerate(buckets)]
        if all(abs(new[j] - centers[j]) < 1e-6 for j in range(k)):
            centers = new
            break
        centers = new
    return sorted(centers), [len(b) for b in buckets]



def slice_and_ocr(img, engine, cols):
    """把整页按列边界切成「标签条 + N 个数值条」分别 OCR，避免相邻列合并。

    切条边界 = **相邻列右缘的中点**（数字右对齐 → 右缘是跨引擎稳定列位）。
    历史修正（2026-10-28）：
      · 曾用 token 跨度中点 → v4 下 "831,229" 被截成 "31229"（token 跨度含前导空格差异）；
      · 曾用 min-left/max-right → 一行多数字时整行 box 横跨多列，col1 读到 78 个数字；
      · 曾加 ±0.012 余量 → 相邻条重叠，同一数字被两列各读一次。
    现口径：右缘中点，无余量，边界天然落在两列之间的空白带。
    """
    from PIL import Image
    W, H = img.size
    n = len(cols)
    lefts = [float(c[0]) for c in cols]
    rights = [float(c[1]) for c in cols]
    # 切条边界 = 相邻两列**数字 token 跨度之间的空隙中点**：
    #   · 用中心中点会截断数字（"486,041" → "486,04"）；
    #   · 用 min-left/max-right 会串列（一行多数字时整行 box 横跨多列）。
    strips = [(0.0, lefts[0], "label")]
    for i in range(n):
        if i == 0:
            left = 0.0
        else:
            left = (rights[i - 1] + lefts[i]) / 2
        if i == n - 1:
            right = 1.0
        else:
            right = (rights[i] + lefts[i + 1]) / 2
        strips.append((left, right, "col%d" % i))
    ocr_by_strip = []
    for left, right, tag in strips:
        if right - left < 0.02:
            ocr_by_strip.append((tag, []))
            continue
        crop = img.crop((int(left * W), 0, int(right * W), H))
        lines = run_page(engine, crop)
        ocr_by_strip.append((tag, lines))
    return ocr_by_strip, [float(c[2]) for c in cols]


def rebuild_rows(ocr_by_strip, col_centers, whole_lines=None):
    """按 y 聚类把各列数值格归行；标签 = 行最左数值格左侧、y 就近的整页文本行。

    v3 修复（真实不合格场景驱动，2026-10-28）：
      · 标签来自整页 OCR（≥2 汉字），按「行首单元格左侧就近」匹配——
        同时支持单栏表与双栏 BS（右栏标签在页面中部也能配对）；
      · y 容差动态（整页行高中位数×0.6，下限 0.012）；
      · 数值唯一分配（同格只归一行）——修「应收票据」重复邻行值的行错位；
      · 全空行丢弃，行按 y 升序。
    """
    def _cjk(s):
        return len(re.findall(r"[\u4e00-\u9fff]", s or ""))

    col_lines = [ls for tag, ls in ocr_by_strip if tag.startswith("col")][:len(col_centers)]

    heights = sorted(l["h"] for l in (whole_lines or [])) or [0.015]
    tol = max(0.012, heights[len(heights) // 2] * 0.6)

    # 数值格（每列：y、值、conf；x 取该列中心，用于标签配对）
    col_cells = []
    for ci, cl in enumerate(col_lines):
        cells = []
        for c in cl:
            v = clean_number(c["text"])
            if v is not None:
                cells.append({"y": c["y"], "v": v, "conf": c["conf"],
                              "x": col_centers[ci] if ci < len(col_centers) else 1.0})
        col_cells.append(sorted(cells, key=lambda c: c["y"]))

    ys = sorted({c["y"] for cells in col_cells for c in cells})
    row_ys = []
    for y in ys:
        if row_ys and abs(y - row_ys[-1]) <= tol:
            continue
        row_ys.append(y)

    # 标签池：整页含 ≥2 汉字的行
    pool = [l for l in (whole_lines or []) if _cjk(l["text"]) >= 2]

    used = [set() for _ in col_cells]
    rows = []
    for ry in row_ys:
        cells, confs, row_left_x = [], [], None
        for ci, cells_list in enumerate(col_cells):
            best = None
            for idx, c in enumerate(cells_list):
                if idx in used[ci]:
                    continue
                d = abs(c["y"] - ry)
                if d <= tol and (best is None or d < best[0]):
                    best = (d, idx, c)
            if best:
                used[ci].add(best[1])
                cells.append(best[2]["v"])
                confs.append(best[2]["conf"])
                x = best[2]["x"]
                row_left_x = x if row_left_x is None else min(row_left_x, x)
            else:
                cells.append(None)
                confs.append(0.0)
        if all(v is None for v in cells):
            continue
        # 标签 = 行首数值格左侧、y 就近的文本行（右缘不超过行首格中心 + 0.02）
        lab, best = "", None
        if row_left_x is not None:
            for p in pool:
                if p["x"] + p["w"] > row_left_x + 0.02:
                    continue  # 必须在行首格左侧
                d = abs(p["y"] - ry)
                if d <= tol * 1.5 and (best is None or d < best[0]):
                    best = (d, p)
        if best:
            lab = best[1]["text"].strip()
        rows.append({"label": lab, "cells": cells,
                     "conf": round(sum(confs) / max(1, len(confs)), 3)})
    return rows


def balance_check(rows, panel=None):
    """BS 平衡勾稽：资产总计 ≈ 负债合计 + 所有者权益合计（逐列，容差 1 元）。

    这是财务三表里最可泛化、最硬的一条自校验：三个总数是从 OCR 里独立读出来的，
    若两边对得上，说明列对齐与数字清洗整体可信；对不上则标出差异交给下游人工核对。
    用锚定精确匹配（^…$），避免「流动资产合计」被「资产合计」子串误配（M1 实测踩过的坑）。

    panel=None：跨面板匹配（双栏 BS：资产总计在左栏，负债/权益在右栏）。
    panel=k：只在第 k 个面板内匹配（单栏表）。
    """
    TOTAL_RE = {
        "assets": re.compile(r"^(总资产|资产总计|资产合计)$"),
        "liab": re.compile(r"^(负债总计|负债合计)$"),
        "equity": re.compile(r"^(所有者权益合计|股东权益合计|所有者权益总计|股东权益总计|所有者权益|净资产)$"),
    }

    def find_total(kind):
        for r in rows:
            if panel is not None and r.get("panel") not in (None, panel):
                continue
            label = re.sub(r"[\s·．.：:]+", "", r["label"])
            if TOTAL_RE[kind].match(label):
                return r
        return None

    assets = find_total("assets")
    liab = find_total("liab")
    equity = find_total("equity")
    if not assets or not liab or not equity:
        return []
    checks = []
    # 双栏时：资产在左栏第 c 列，负债/权益在右栏第 c 列 → 按面板内列序对齐
    n = min(len(assets["cells"]), len(liab["cells"]), len(equity["cells"]))
    for ci in range(n):
        a, l, e = assets["cells"][ci], liab["cells"][ci], equity["cells"][ci]
        if a is not None and l is not None and e is not None:
            diff = round(abs(a - (l + e)), 2)
            checks.append({
                "col": ci,
                "assets_total": a,
                "liab_plus_equity": round(l + e, 2),
                "diff": diff,
                "balanced": diff <= 1,
            })
    return checks


def _numeric_token_x(lines, y_min=0.18, label_max=0.16, x_max=0.95):
    """抽取页面里所有数字 token 的 (锚点x, 跨度left, 跨度right)。"""
    out = []
    for l in lines:
        if l["y"] < y_min or l["x"] > x_max:
            continue
        text = l["text"]
        if len(re.findall(r"[\u4e00-\u9fff]", text)) >= 10:
            continue
        for m in NUM_RE.finditer(text):
            if clean_number(m.group(0)) is None:
                continue
            fs = m.start() / max(1, len(text))
            fe = m.end() / max(1, len(text))
            cx = l["x"] + l["w"] * fs
            if cx >= label_max:
                out.append((cx, l["x"] + l["w"] * fs, l["x"] + l["w"] * fe))
    return out


def _columns_from_tokens(tokens, k=None):
    """由数字 token 集合聚类出列 [(left, right, center)]。

    归属 = **唯一最近中心**（显式 argmin，不用浮点相等比较，
    否则同一 token 会同时落进多个簇 → 列跨度重叠 → 勾稽不平，PP-OCRv6 实测）。

    k=None 时按「每簇样本≥3 且簇内跨度窄」自动选列数（2..6）：
    数字右对齐 → 真列的 token 跨度窄；k 过小会把两列并成一簇（跨度变宽），
    k 过大则出现空簇/碎簇。FDD p37 = 3 列、星环资产栏 = 2 列，都由此自适应。
    """
    xs = [t[0] for t in tokens]
    # 列数固定 3（已验证口径：FDD p37 三线表 期末/期初 3 列）。
    # 曾尝试按「簇内跨度」自动选 k，但 token 跨度是字符比例估算（不精确），
    # 会在 k=6 时把一个数字拆成多段（"608,209" → 608/209），2026-10-28 实测失败。
    k = k if k and 0 < k <= 6 else 3
    centers, _ = kmeans1d(xs, k=k)
    centers = [float(c) for c in centers]
    buckets = [[] for _ in centers]
    for cx, tl, tr in tokens:
        j = min(range(len(centers)), key=lambda t: abs(cx - centers[t]))
        buckets[j].append((tl, tr))
    cols = []
    for j, c in enumerate(centers):
        if buckets[j]:
            left = min(e[0] for e in buckets[j])
            right = max(e[1] for e in buckets[j])
        else:
            left, right = max(0.0, c - 0.04), min(1.0, c + 0.04)
        cols.append((round(float(left), 4), round(float(right), 4), round(float(c), 4)))
    return cols


_GEO_MOD = None
_GEO_TRIED = False


def _geo_module():
    """加载同目录的 geo_table（**模块级缓存**：此前每页 exec_module 一次，纯开销）。

    按**同目录绝对路径**加载：core 可能被 importlib 从绝对路径加载
    （khlib.ocr_api._load_plugin_core 的做法），此时 sys.path 里**没有 core/**
    ⇒ 直接 `from geo_table import ...` 会静默失败并悄悄退回切条法
    （2026-11-14 实测：khlib 路径下几何法未生效，数字又被切碎）。
    """
    global _GEO_MOD, _GEO_TRIED
    if _GEO_TRIED:
        return _GEO_MOD
    _GEO_TRIED = True
    try:
        import importlib.util as _ilu
        _geo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geo_table.py")
        _spec = _ilu.spec_from_file_location("dsh_ocr_geo_table", _geo_path)
        _geo = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_geo)
        _GEO_MOD = _geo
    except Exception:                                  # noqa: BLE001
        _GEO_MOD = None
    return _GEO_MOD


def _table_extract_geo(engine, img, whole=None):
    """几何重建兜底（只用 OCR 精确 bbox，不切图 ⇒ 数字不会被截断）。"""
    _geo = _geo_module()
    if _geo is None:
        return {"ok": False, "reason": "几何重建模块不可用：加载失败"}
    reconstruct = _geo.reconstruct
    try:
        lines = whole if whole is not None else run_page(engine, img)
        r = reconstruct(lines, clean_number=clean_number, num_re=NUM_RE)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "reason": "几何重建失败：%s" % exc}
    if not r.get("ok"):
        return r
    rows = []
    for pl in (r.get("panels") or []):
        for row in (pl.get("rows") or []):
            if not row.get("has_value"):
                continue
            rows.append({"label": row["label"], "cells": row.get("cells") or [],
                         "panel": row.get("panel", 0)})
    checks = balance_check(rows) if rows else []
    return {"ok": True, "columns": len(r.get("columns") or []),
            "column_list": r.get("columns") or [],
            "columns_total": len(r.get("columns") or []),
            "panel_count": int(r.get("panel_count") or 1),
            "panel_evidence": r.get("panel_evidence") or {},
            "panels": [{"panel": p.get("panel"), "columns": p.get("columns"),
                        "x_range": p.get("x_range")} for p in (r.get("panels") or [])],
            "rows": rows, "balance_checks": checks,
            "geo": True, "orphan_values": r.get("orphans") or [],
            "lines": whole}


def _coord_source(whole):
    """这批坐标的来源：'text_layer'（精确、零 OCR 成本）| 'ocr'（扫描件）。

    判别依据：RapidOCR 的 `run_page` 产出的 lines **无 `source` 键**
    （只有 x/y/w/h/text/conf）；而 khlib/text_layer_bbox.py 产出的词级坐标
    带 `source="text_layer"` ⇒ 精确判别，不靠猜。
    """
    for l in (whole or [])[:8]:
        s = l.get("source")
        if s:
            return str(s)
    return "ocr"


def table_extract(engine, img, whole=None):
    """table 模式主流程：整页 OCR → **先分栏** → 各栏定列 → 切条重 OCR → 归行 → 勾稽。

    `whole`：可选的**整页 OCR 行**（缓存命中时由调用方传入 ⇒ 省掉一次整页 OCR）。
             页级 OCR 结果与模式无关（text / table 的整页 OCR 是同一份产物），
             故调用方可用同一份缓存喂进来。

    2026-10-28 升级：
      · 行重建 v3（整页标签 + 数值唯一分配）→ 修「应收票据抓到交易性金融资产」行错位；
      · 数值 token 跨度口径 + 唯一最近中心归属 → 修 v6 下相邻列串列；
      · **先分栏再定列** → 支持审计报告双栏 BS（左资产 / 右负债权益），
        且不把 FDD p37 右侧「客户集中度」小表混进 BS 列（已验证口径不变）。

    2026-11-15 提速（实测 p6 单页 5.50s → 1.30s）：
      · **几何前置早退**：几何重建只复用整页 OCR 的行（0 次额外 OCR，实测 0.00s），
        而切条重 OCR 占 77% 的时间（4.20s / 4 次调用）。
        原顺序是「切条 → 才看几何」，导致**最终用几何结果但切条的钱已白付**。
        现在几何若算出 **2 列**就立即返回（判据原就存在，只是位置错了）。
    """
    whole = whole if whole is not None else run_page(engine, img)
    tokens = _numeric_token_x(whole)
    if len(tokens) < 4:
        # 几何兜底：整页 OCR 数值 token 少但版面可能有表（如审计报告宽表）
        geo = _table_extract_geo(engine, img, whole)
        if geo.get("ok"):
            return geo
        return {"ok": False, "reason": "未检测到足够的数值列（可能非表格页）", "lines": whole,
                "route": {"decision": "none", "reason": "整页数值 token 不足 ⇒ 非表格页",
                          "coord_source": _coord_source(whole)}}
    # ★★ 路由（2026-11-15）：**全部由代码判定**，判据与依据都写进 `route` 字段（可审计）。
    #
    #   判定输入（全部是**测量值**，不是注释里的口头规则）：
    #     · `src`      坐标来源：text_layer（精确词级 bbox）| ocr（扫描件）
    #     · `geo_cols` 几何法算出的有效数值列数
    #     · `panels_n` 几何法算出的**栏数**（`geo_table.detect_panels`）
    #
    #   决策表：
    #     ┌─────────────────┬──────────┬──────────────────────────────────────────┐
    #     │ 条件             │ 走哪条    │ 依据                                     │
    #     ├─────────────────┼──────────┼──────────────────────────────────────────┤
    #     │ src=text_layer  │ 几何      │ 精确坐标：列数自适应、不切条、不截断      │
    #     │ 扫描件 + 栏数≥2  │ 切条      │ 切条法按栏重 OCR（每栏 3 列，已验证）    │
    #     │ 扫描件 + 单栏+2列│ 几何      │ 切条硬编码 3 列 ⇒ 必碎（实测已证）      │
    #     │ 扫描件 其它      │ 切条      │ 已验证口径                               │
    #     └─────────────────┴──────────┴──────────────────────────────────────────┘
    #
    #   ★ 为什么「扫描件 + 双栏」必须走切条（2026-11-15 实测）：
    #     几何法**已能分栏**（detect_panels 判据：最大间隙处切分且两侧各 ≥2 个列簇，
    #     5 个样本全判对；旧判据用标签间隙，既漏判<项目A>双栏 BS、又把序号列误当分栏），
    #     但**双栏 BS 的几何重建仍有多余行**（实测<项目A> p37 出现负值重复行）⇒ 未验证 ⇒ 保持切条。
    geo = _table_extract_geo(engine, img, whole)
    geo_cols = len(geo.get("column_list") or []) if geo.get("ok") else 0
    panels_n = int(geo.get("panel_count") or 1) if geo.get("ok") else 1
    src = _coord_source(whole)
    route = {"coord_source": src, "columns": geo_cols, "panels": panels_n,
             "panel_evidence": geo.get("panel_evidence") or {}}
    if geo.get("ok"):
        if src == "text_layer":
            route.update(decision="geometry",
                         reason="文本层精确坐标：几何重建（列数自适应、不切条不截断）")
            geo["route"] = route
            geo["fallback_reason"] = route["reason"]
            return geo
        if panels_n >= 2:
            route.update(decision="slice",
                         reason="双栏表：切条法按栏重 OCR（每栏 3 列，已验证口径）；"
                                "几何法虽已能分栏但双栏 BS 重建仍有多余行（未验证）")
            geo["route"] = route
            # ↓ 继续走切条法（下面的代码）
        elif geo_cols != 3:
            # 单栏且列数 ≠ 3 ⇒ 切条法**必然切碎数字**（它只支持 3 列）⇒ 几何。
            #   2 列（审计报告合并 BS）与 ≥4 列（多年度明细表）都归到这里。
            route.update(decision="geometry",
                         reason="单栏 %d 列：切条法列数硬编码 3 会切碎数字，改用几何重建"
                                % geo_cols)
            geo["route"] = route
            geo["fallback_reason"] = route["reason"]
            return geo
        else:
            route.update(decision="slice", reason="扫描件已验证口径（切条法）")
            geo["route"] = route
    else:
        # ★ 保留 geo 的**具体**失败原因（审计链不丢关键一环）
        _gr = geo.get("reason") or "几何重建不可用"
        route.update(decision="slice", reason="几何重建不可用（%s）⇒ 切条法" % _gr)
    # ① 按 x 空白带把页面切成栏区（审计报告双栏 BS：左资产 x<0.45 / 右负债权益 x>0.5）
    # ① 分栏：**唯一判据来源 = 几何法的 `detect_panels`**（`geo_table`，与 khlib 分类器共用）
    #   ★ 2026-11-15 评审修正：此前切条路径**自带一套**分栏判据
    #     （token x 间隙 >0.25 + 每侧 ≥8 token），与 `geo_table.detect_panels`
    #     （右缘间隙 + 每侧 ≥2 列簇）**并存且互不引用** ⇒ 两套可给出相反结论：
    #     路由判「双栏」走切条、切条自己却判「不分栏」⇒ 左右栏混算 ⇒ 勾稽对不上
    #     —— 正是这套改造要修的失败模式。
    #   ⇒ 现在切条路径直接**消费**几何法的分栏结果（同一判据、同一份实现）。
    #      几何法不可用时不分栏（保守：整本按单栏处理）。
    split_at = None
    if geo.get("ok") and int(geo.get("panel_count") or 1) >= 2:
        split_at = (geo.get("panel_evidence") or {}).get("split_at")
    panel_tokens = [[], []]
    if split_at is None:
        panel_tokens[0] = tokens
    else:
        # ★ 分栏归属用 token 的**右缘**（`tr`），与 geo 的分栏基准一致
        #   （geo 用列的 right edge 切；token 的锚点 cx 是数字**起点**，
        #    右栏的宽数字其起点可能越过分界 ⇒ 错栏。
        #   `tr` 是该 token 的估算右缘 ⇒ 与 geo 同基准。复审残留项，已修。）
        for t in tokens:
            panel_tokens[0 if (t[2] if len(t) > 2 else t[0]) < split_at else 1].append(t)

    all_rows, all_checks, panel_info = [], [], []
    for pi, ptoks in enumerate(panel_tokens):
        if len(ptoks) < 4:
            continue
        pcols = _columns_from_tokens(ptoks)
        if len(pcols) < 2:
            continue
        ocr_by_strip, centers = slice_and_ocr(img, engine, pcols)
        rows = rebuild_rows(ocr_by_strip, centers, whole_lines=whole)
        for r in rows:
            r["panel"] = pi
        all_rows.extend(rows)
        panel_info.append({"panel": pi, "columns": len(pcols),
                           "x_range": [round(float(pcols[0][0]), 4), round(float(pcols[-1][1]), 4)]})
    if not all_rows:
        return {"ok": False, "reason": "未能解析出表格行", "lines": whole,
                "route": {"decision": "slice", "reason": "切条法未解析出表格行",
                          "coord_source": _coord_source(whole)}}
    # 注：几何重建的早退已在函数入口处完成（`geo==2` 直接返回，省掉切条 OCR）；
    #     走到这里的都是「几何没算出 2 列」的页（如 3 列窄表 FDD p37），继续按切条法口径产出。
    # 勾稽：单栏 → 栏内勾稽；多栏 → 跨栏勾稽（资产在左栏、负债/权益在右栏）
    if len(panel_info) <= 1:
        all_checks = [dict(c, panel=0) for c in balance_check(all_rows)]
    else:
        all_checks = balance_check(all_rows)
        if not all_checks:
            def pick(kind, panel):
                for r in all_rows:
                    if r.get("panel") != panel:
                        continue
                    if re.match(kind, re.sub(r"[\s·．.：:]+", "", r["label"])):
                        return r
                return None
            assets = pick(r"^(总资产|资产总计|资产合计)$", 0)
            liab = pick(r"^(负债总计|负债合计)$", 1)
            equity = pick(r"^(所有者权益合计|股东权益合计|所有者权益总计|股东权益总计)$", 1)
            if assets and liab and equity:
                n = min(len(assets["cells"]), len(liab["cells"]), len(equity["cells"]))
                for ci in range(n):
                    a, l, e = assets["cells"][ci], liab["cells"][ci], equity["cells"][ci]
                    if a is not None and l is not None and e is not None:
                        diff = round(abs(a - (l + e)), 2)
                        all_checks.append({"col": ci, "assets_total": a,
                                           "liab_plus_equity": round(l + e, 2),
                                           "diff": diff, "balanced": diff <= 1,
                                           "cross_panel": True})
    # columns 报**主面板列数**（默认第一栏），与既有口径一致；
    # columns_total 给全页列数合计，panels 给分栏明细。
    main_cols = panel_info[0]["columns"] if panel_info else 0
    return {"ok": True, "columns": main_cols,
            "columns_total": sum(p["columns"] for p in panel_info), "panels": panel_info,
            "rows": all_rows, "balance_checks": all_checks, "lines": whole,
            # 路由决策（可审计：为什么这一页走了切条法）
            "route": dict(route, slice_panels=len(panel_info)),
            "fallback_reason": route.get("reason")}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="dsh-ocr 引擎")
    ap.add_argument("--input", required=True)
    ap.add_argument("--pages", default="all")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--mode", default="text",
                    choices=["text", "table", "clause", "legal", "strategy"],
                    help="text=整页文本；table=财务表格；clause=交易条款；legal=法律尽调扫描；strategy=战略报告扫描")
    ap.add_argument("--lang", default="ch")
    ap.add_argument("--out-json")
    ap.add_argument("--out-md")
    ap.add_argument("--probe", action="store_true", help="只检测原生扩展能否加载（不跑 OCR）")
    args = ap.parse_args()

    # probe：给 runner/插件判断「这个解释器能不能跑 OCR」，不依赖输入文件
    if args.probe:
        probe = {"ok": True, "python": sys.executable, "imports": {}, "errors": []}
        for mod in ("onnxruntime", "cv2", "pypdfium2", "rapidocr_onnxruntime"):
            try:
                __import__(mod)
                probe["imports"][mod] = True
            except Exception as e:
                probe["imports"][mod] = False
                probe["errors"].append("%s: %s" % (mod, str(e).splitlines()[0][:200]))
        probe["ok"] = len(probe["errors"]) == 0
        print(json.dumps(probe, ensure_ascii=False))
        return

    if not os.path.exists(args.input):
        emit_error("input not found: %s" % args.input)
        return

    try:
        engine = load_engine(args.lang)
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": "engine_unavailable",
                          "detail": str(e)}, ensure_ascii=False))
        return
    t0 = time.time()
    ext = os.path.splitext(args.input)[1].lower()

    # 先解析页数（PDF 需要先打开取总数）
    total_pages = 1
    if ext not in IMG_EXTS:
        try:
            import pypdfium2 as pdfium
            total_pages = len(pdfium.PdfDocument(args.input))
        except Exception as e:
            emit_error("PDF 打开失败: %s" % e)
            return
    pages = resolve_pages(args.pages, total_pages)

    images = load_images(args.input, pages, args.dpi)
    per_page = []
    all_text_lines = []
    for pno, img, meta in images:
        lines = run_page(engine, img)
        confs = [l["conf"] for l in lines]
        txt = page_text(lines)
        page_rec = {
            "page": pno,
            "chars": len(txt),
            "lines": len(lines),
            "avg_conf": round(sum(confs) / len(confs), 3) if confs else 0.0,
            "text": txt,
        }
        if args.mode == "table":
            te = table_extract(engine, img)
            page_rec["table"] = te
        per_page.append(page_rec)
        all_text_lines.append((pno, lines))

    clauses = clause_hits(all_text_lines, SCAN_PATTERNS.get(args.mode)) \
        if args.mode in SCAN_PATTERNS else []

    out = {
        "ok": True,
        "engine": getattr(engine, "_dsh_engine", "RapidOCR"),
        "input": args.input,
        "mode": args.mode,
        "dpi": args.dpi,
        "pages_requested": pages,
        "page_count": len(images),
        "elapsed_s": round(time.time() - t0, 1),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pages": per_page,
        "clauses": clauses,
    }
    if args.out_json:
        write_json(args.out_json, out)
    if args.out_md:
        write_md(args.out_md, out)
    print(json.dumps(out, ensure_ascii=False))


def emit_error(msg):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))


def write_json(path, out):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


def write_md(path, out):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    L = ["# OCR 结果 · %s\n" % os.path.basename(out["input"]),
         "- 引擎：%s" % out["engine"],
         "- 模式：%s · DPI %s · 页数 %s · 耗时 %ss\n" % (out["mode"], out["dpi"], out["page_count"], out["elapsed_s"])]
    for p in out["pages"]:
        L.append("\n## 第 %s 页（%s 行 · conf %s）\n" % (p["page"], p["lines"], p["avg_conf"]))
        if out["mode"] == "table" and p.get("table", {}).get("ok"):
            t = p["table"]
            L.append("| 项目 | %s |" % " | ".join("列%d" % i for i in range(t["columns"])))
            L.append("|---|" + "---|" * t["columns"])
            for r in t["rows"]:
                cells = ["%s" % (("" if v is None else format_number(v))) for v in r["cells"]]
                L.append("| %s | %s |" % (r["label"], " | ".join(cells)))
        else:
            L.append("```")
            L.append(p["text"])
            L.append("```")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def format_number(v):
    if abs(v - round(v)) < 1e-9:
        return str(int(v))
    return "%.4f" % v


if __name__ == "__main__":
    main()
