# sitecustomize.py —— 由 dsh-ocr/runner.js 通过 PYTHONPATH 注入
#
# 作用：把 DSH 的 site-packages **追加**到 sys.path 末尾。
# 为什么是追加而不是 PYTHONPATH 前置：
#   Anaconda 自带的 numpy/pandas 是按 NumPy 1.x 编译的；若把 DSH site-packages
#   前置，会优先载入 DSH 的 NumPy 2.3.5，导致 pyarrow/numexpr/bottleneck 报
#   "_ARRAY_API not found"（ABI 不兼容），pandas 启动刷屏。
#   追加到末尾 = Anaconda 自己的包优先（互相匹配），DSH 只补 Anaconda 缺失的包
#   （本插件需要的是 rapidocr_onnxruntime / onnxruntime / cv2 / pypdfium2 等
#   带 C 扩展、DSH 内置 python 因代码签名无法加载的包）。
import os
import sys

# ── ① 插件自带稳定依赖目录 _deps（优先级最高，且**必须**在 DSH site-packages 之前）
#
# 为什么需要这一段：runner.js 的 extraSite 只指向 DSH 内置 site-packages
# （dshSitePackages()），而 rapidocr/onnxruntime/cv2/pypdfium2 实际装在插件目录的
# _deps/ 里（314 MB）—— 2026-11-14 复测发现 _deps **从未被加入 sys.path**，
# 于是 health_check 报「No module named 'rapidocr'」，而手动 sys.path.append 却能跑通。
# 这就是「工具在、引擎却说不可用」的真实成因（不是依赖没装）。
#
# 放在 DSH site-packages **之前**：_deps 是成套钉版装的（numpy 2.5.4 / cv2 5.0 /
# onnxruntime 1.31.1 互相匹配），若让 DSH 的包先命中会重演 NumPy ABI 冲突。
_deps = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "_deps")
if os.path.isdir(_deps) and _deps not in sys.path:
    sys.path.insert(0, _deps)

_sp = os.environ.get("V3_EXTRA_SITE")
if _sp and os.path.isdir(_sp) and _sp not in sys.path:
    if os.environ.get("V3_PREFER") == "extra":
        sys.path.insert(0, _sp)      # 备用：让 extras 优先（会引入 NumPy ABI 噪音）
    else:
        sys.path.append(_sp)
