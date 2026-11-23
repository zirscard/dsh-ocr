"""MiniCPM-OCR Jev：OCR 结果正确性评分器（可选增强层）。

来源：https://github.com/OpenBMB/MiniCPM-OCR（Apache-2.0）
模型：openbmb/MiniCPM-OCR-Jev（2B，Qwen3.5-2B 骨干 + 二分类头 score.weight [2,2048]）

职责：给「引擎读出来的数字/文本」打分（0–1，越高越可能正确），用于
      · 自动接受高置信区域；
      · 把可疑区域路由到人工复核（纪律：冲突或低置信 → requires_human_review）；
      · 多个 OCR 引擎的结果择优（v4 vs v6 选 Jev 分高者）。

与现有勾稽的关系：BS 勾稽（balance_check）是**确定性规则**、零成本、可复算；
Jev 是**模型化复核**，两者互补而非替代——勾稽管"数字对不对"，Jev 管"这串字读对了吗"。

离线/未安装时：本模块不报错，由调用方捕获后降级为「仅勾稽」模式。
"""
import os
import sys

MODEL_ID = os.environ.get("DSH_JEV_MODEL", "openbmb/MiniCPM-OCR-Jev")

# 离线守门（成本控制，2026-11-01 起 DeepSeek 搜索 API 计费）：
# 默认**禁止任何联网动作**——权重只从本地缓存读，缺失就报 jev_unavailable，
# 需要下载时必须显式 DSH_JEV_ALLOW_DOWNLOAD=1（避免自动化流程悄悄产生网络/计费动作）。
ALLOW_DOWNLOAD = os.environ.get("DSH_JEV_ALLOW_DOWNLOAD", "0") == "1"
# 本机 huggingface.co 不可达时走镜像（实测 hf-mirror.com 可用）
if not os.environ.get("HF_ENDPOINT"):
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def available():
    """检查依赖与权重是否就绪（不触发下载）。"""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except Exception:
        return False, "torch/transformers 未安装"
    try:
        from huggingface_hub import snapshot_download
        cache = os.environ.get("DSH_JEV_CACHE", "/tmp/hf-cache")
        if not ALLOW_DOWNLOAD:
            cache = os.environ.get("DSH_JEV_CACHE", "/tmp/hf-cache")
        # local_files_only 时必须给 allow_patterns，否则 HF 会因缺 blob 校验而报
        # IncompleteSnapshotError（快照目录里是符号链接）
        path = snapshot_download(MODEL_ID, local_files_only=True, cache_dir=cache,
                                allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"])
        if not os.path.exists(os.path.join(path, "model.safetensors")):
            return False, "权重不完整（缺 model.safetensors）"
        return True, path
    except Exception as e:
        return False, "权重未下载（%s: %s）" % (type(e).__name__, str(e)[:120])


class JevScorer(object):
    """Jev 打分器：区域图 + 候选 OCR 文本 → 0~1 正确性分数。"""

    def __init__(self, model_id=None, cache_dir=None, threads=2, device=None):
        self.model_id = model_id or MODEL_ID
        self.cache_dir = cache_dir or os.environ.get("DSH_JEV_CACHE", "/tmp/hf-cache")
        self.threads = threads
        # 设备选择：默认优先 MPS（Apple GPU），不可用则回落 CPU。
        # 2026-10-29 实测（M2 Pro）：CPU 整页 ~20 分钟 → MPS 整页 **19 秒**（约 60×）；
        # 小区域裁剪 CPU 57s → MPS 1.1s。判别力不变（好 0.73 / 坏 0.0001）。
        # causal-conv1d 在 macOS arm64 **无法构建**（CUDA 专用）；
        # flash-linear-attention 的 fla-core[cpu] 依赖 triton>=3.3（macOS 无 wheel）→ 均装不了，
        # 所以 GPU(MPS) 是本机唯一可行的加速路径。
        # DSH_JEV_DEVICE=cpu|mps|auto 可强制。
        self.device = device or os.environ.get("DSH_JEV_DEVICE", "auto")
        self._model = None
        self._tok = None
        self._proc = None
        self._head = None
        self._device_obj = None

    def _pick_device(self):
        """返回 (torch.device, 说明)。"""
        import torch
        want = (self.device or "auto").lower()
        if want in ("cpu",):
            return torch.device("cpu"), "CPU（强制）"
        if want == "mps":
            if not (torch.backends.mps.is_available() and torch.backends.mps.is_built()):
                return torch.device("cpu"), "CPU（MPS 不可用，已回落）"
            return torch.device("mps"), "MPS（Apple GPU）"
        if torch.backends.mps.is_available() and torch.backends.mps.is_built():
            return torch.device("mps"), "MPS（Apple GPU，自动）"
        return torch.device("cpu"), "CPU（无 MPS，自动回落）"

    # -- 内部 ---------------------------------------------------------------
    def _ensure(self):
        if self._model is not None:
            return True
        import torch
        from transformers import AutoModel, AutoTokenizer
        from huggingface_hub import snapshot_download
        if not ALLOW_DOWNLOAD:
            # 只用本地缓存；缺权重就明确失败（不触发任何联网/计费动作）
            path = snapshot_download(self.model_id, cache_dir=self.cache_dir, local_files_only=True,
                                    allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"])
        else:
            path = snapshot_download(self.model_id, cache_dir=self.cache_dir,
                                    allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"])
        torch.set_num_threads(max(1, self.threads))
        dev, why = self._pick_device()
        self._device_obj = dev
        self._device_note = why
        self._tok = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
        self._model = AutoModel.from_pretrained(path, trust_remote_code=True,
                                                torch_dtype="auto").eval()
        if dev.type == "mps":
            # MPS 上 bf16 算子覆盖不全，统一转 fp32 稳妥；显存换速度
            self._model = self._model.to(device="mps", dtype=torch.float32)
        # 处理器用 AutoProcessor 单独加载（该模型类没有 get_processor 方法）
        from transformers import AutoProcessor
        try:
            self._proc = AutoProcessor.from_pretrained(path, trust_remote_code=True)
        except Exception:
            self._proc = None
        # 二分类头：score.weight [2, H]
        try:
            from safetensors.torch import load_file
            sd = load_file(os.path.join(path, "model.safetensors"))
            w = sd.get("score.weight")
            if w is not None:
                self._head = w.float()
                if self._device_obj is not None and self._device_obj.type == "mps":
                    self._head = self._head.to(self._device_obj)
        except Exception:
            self._head = None
        return True

    # -- 对外 ---------------------------------------------------------------
    def _build_inputs(self, image, candidate_text):
        """Jev 输入 = 原区域图 + 候选文本 + 「文本渲染图」。

        模型卡：内部使用「原区域图 + 候选文本 + 由该文本渲染出的图像」三件套；
        无内置渲染器时由调用方提供渲染图，这里用 PIL 渲染候选文本。
        提示词手工拼（该模型无 chat_template，是二分类模型不是生成模型）。
        """
        from PIL import Image, ImageDraw
        try:
            lines = candidate_text.split("\n")[:40]
            lh, W = 22, 1200
            rend = Image.new("RGB", (W, max(lh, len(lines) * lh)), "white")
            dr = ImageDraw.Draw(rend)
            for i, ln in enumerate(lines):
                dr.text((6, 4 + i * lh), ln[:80], fill="black")
        except Exception:
            rend = image
        # 降采样：Jev 判的是「这串字读对了吗」，不需要全分辨率；
        # 原图 2 张会产生 2000+ vision token，2B 模型在 CPU 上要跑半小时（实测）。
        max_side = int(os.environ.get("DSH_JEV_MAX_SIDE", "896"))
        def _down(im):
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_side:
                sc = max_side / float(max(w, h))
                im = im.resize((max(1, int(w * sc)), max(1, int(h * sc))))
            return im
        image = _down(image)
        rend = _down(rend)
        # 占位符个数必须与图片个数一致（Qwen3VL processor 校验）→ 2 图 2 占位
        V = "<|vision_start|><|image_pad|><|vision_end|>"
        prompt = (
            "<|im_start|>user\n"
            + V + "Original document region image.\n"
            + V + "Candidate OCR result rendered as an image.\n"
            "Candidate OCR text:\n%s\n"
            "Is the candidate OCR result correct for the original region?\n"
            "<|im_end|>\n<|im_start|>assistant\n" % candidate_text[:4000]
        )
        return self._proc(text=[prompt],
                          images=[image.convert("RGB"), rend.convert("RGB")],
                          return_tensors="pt")

    def score(self, image, candidate_text, category="table"):
        """返回 (score, detail)。score∈[0,1]；失败返回 (None, 原因)。"""
        if not candidate_text or not candidate_text.strip():
            return None, "候选文本为空"
        try:
            import torch
            self._ensure()
            inputs = self._build_inputs(image, candidate_text)
            if self._device_obj is not None and self._device_obj.type == "mps":
                inputs = {k: (v.to(self._device_obj) if hasattr(v, "to") else v)
                          for k, v in inputs.items()}
            with torch.no_grad():
                # logits_to_keep=1：只要最后 token 的输出，避免为全序列算 logits
                out = self._model(**inputs, output_hidden_states=True, return_dict=True,
                                  logits_to_keep=1)
            hs = out.hidden_states[-1][0][-1].float()   # 最后 token 的隐状态
            if hs.device != torch.device("cpu"):
                hs = hs.cpu()
                if self._head is not None and self._head.device != torch.device("cpu"):
                    self._head = self._head.cpu()
            if self._head is None:
                return None, "缺少 score.weight 分类头"
            logits = self._head @ hs.float()          # [2]
            prob = torch.softmax(logits, dim=-1)
            s = float(prob[1]) if prob.numel() > 1 else float(prob[0])
            return round(s, 4), {"category": category, "device": getattr(self, "_device_note", "?"),
                                 "logits": [round(float(x), 4) for x in logits]}
        except Exception as e:
            return None, "%s: %s" % (type(e).__name__, str(e)[:200])


def crop_table_regions(img, lines=None, pad=0.012, max_regions=8):
    """按 OCR 行框把页面切成「表格区域」小图。

    Jev 是**区域级**正确性模型（模型卡：assessment unit = one text/formula/table
    region plus its candidate OCR result）。实测：
      · 整页 A4@300dpi 下采样到 448~1280 → good/bad 分数都塌到 0.002~0.005（无区分度）
      · 裁到单个小表区域 → good 0.73 / bad 0.0001（区分度清晰）
    所以调用方应**先切区域再评分**，而不是整页喂。

    切法：取「数值密集」的行（≥3 个数字）→ 按纵向连续性聚成**表格带** →
    每个带按不超过 max_rows 行再分块（太小则合并相邻块）。
    注意：ocr.run_page 的 x/y/w/h 是**归一化到 0..1** 的坐标。
    """
    from PIL import Image
    W, H = img.size
    if not lines:
        return [img]
    num_rows = []
    for l in lines:
        t = l.get("text", "") or ""
        if sum(ch.isdigit() for ch in t) >= 3:
            num_rows.append(l)
    if len(num_rows) < 3:
        return [img]
    num_rows.sort(key=lambda l: l["y"])

    # 1) 聚成表格带：行距小于 ~2.5 倍行高即视为同一带
    bands, cur = [], [num_rows[0]]
    for l in num_rows[1:]:
        if l["y"] - cur[-1]["y"] <= max(0.010, l["h"] * 2.5):
            cur.append(l)
        else:
            bands.append(cur); cur = [l]
    bands.append(cur)

    # 2) 每个带按行数上限切块（每块至少 3 行，否则合并到相邻块）
    MAX_ROWS = 6
    blocks = []
    for b in bands:
        if len(b) <= MAX_ROWS:
            blocks.append(b); continue
        for i in range(0, len(b), MAX_ROWS):
            blocks.append(b[i:i + MAX_ROWS])
    # 3) 合并过小的块
    merged = []
    for b in blocks:
        if merged and len(merged[-1]) < 3:
            merged[-1] = merged[-1] + b
        else:
            merged.append(b)
    blocks = [b for b in merged if len(b) >= 3]
    if not blocks:
        return [img]

    out = []
    for b in blocks[:max_regions]:
        nx0 = max(0.0, min(l["x"] for l in b) - pad)
        ny0 = max(0.0, min(l["y"] for l in b) - pad)
        nx1 = min(1.0, max(l["x"] + l["w"] for l in b) + pad)
        ny1 = min(1.0, max(l["y"] + l["h"] for l in b) + pad)
        box = (int(nx0 * W), int(ny0 * H), int(nx1 * W), int(ny1 * H))
        if box[2] - box[0] >= 80 and box[3] - box[1] >= 40:
            out.append(img.crop(box))
    return out or [img]


def main():
    import argparse
    ap = argparse.ArgumentParser(description="MiniCPM-OCR Jev 正确性评分")
    ap.add_argument("--input", required=True, help="图片或 PDF（取第一页）")
    ap.add_argument("--text", default="", help="候选 OCR 文本（缺省用引擎现读结果）")
    ap.add_argument("--category", default="table", choices=["text", "formula", "table"])
    ap.add_argument("--regions", action="store_true",
                    help="按表格区段裁剪后逐区评分（Jev 是区域级模型；整页喂会失去区分度）")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--out-json", default="")
    args = ap.parse_args()

    ok, info = available()
    if not ok:
        out = {"ok": False, "error": "jev_unavailable", "detail": info,
               "hint": "下载权重：HF_ENDPOINT=https://hf-mirror.com python -c "
                       "\"from huggingface_hub import snapshot_download; "
                       "snapshot_download('%s', cache_dir='/tmp/hf-cache')\"" % MODEL_ID}
        print(__import__("json").dumps(out, ensure_ascii=False))
        return 0 if args.out_json else 1

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ocr

    ext = os.path.splitext(args.input)[1].lower()
    if ext == ".pdf":
        pno, img, meta = ocr.load_images(args.input, [1], args.dpi)[0]
    else:
        from PIL import Image
        img = Image.open(args.input).convert("RGB")
    text = args.text
    if not text:
        eng = ocr.load_engine("ch")
        text = ocr.page_text(ocr.run_page(eng, img))

    scorer = JevScorer()
    if args.regions:
        eng = ocr.load_engine("ch")
        lines = ocr.run_page(eng, img)
        crops = crop_table_regions(img, lines)
        results = []
        for i, c in enumerate(crops):
            t = ocr.page_text(ocr.run_page(eng, c))
            sc, d = scorer.score(c, t, category=args.category)
            # score() 失败时 detail 是字符串（原因），成功时是 dict
            det = d if isinstance(d, dict) else {"reason": d}
            results.append({"region": i, "size": list(c.size), "score": sc,
                            "logits": det.get("logits"), "chars": len(t),
                            "device": det.get("device")})
        good = [r["score"] for r in results if r["score"] is not None]
        out = {"ok": bool(good), "regions": results, "region_count": len(results),
               "min_score": min(good) if good else None,
               "max_score": max(good) if good else None,
               "input": args.input}
    else:
        score, detail = scorer.score(img, text, category=args.category)
        out = {"ok": score is not None, "score": score, "detail": detail,
               "input": args.input, "chars": len(text)}
    import json
    print(json.dumps(out, ensure_ascii=False))
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False))
    if args.regions:
        # 区域模式：至少有一个区拿到分就算成功
        return 0 if out.get("ok") else 1
    return 0 if score is not None else 1


if __name__ == "__main__":
    sys.exit(main())
