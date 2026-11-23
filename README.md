# dsh-ocr · 通用文档扫描识别插件

把 **OCR 做成可复用的 DSH 插件**：agent 工具 **`doc_ocr`** 把 PDF / 图片转成结构化内容（逐页文本 / 表格结构 / 关键词命中）。

**Domain-agnostic（领域无关）**：本插件不假设文档「是什么」——财务报表、合同、票据、表单、论文、扫描件一视同仁。
业务化的下游流程（谁消费、怎么裁决、产出落到哪里）属于**调用方的技能与流水线**，不在这里。

引擎 **RapidOCR**（PaddleOCR PP-OCRv6 ONNX）：**全本地推理、零联网、零 API 费用**，中文精度实测 conf 0.95+。

## 一、三种模式

| mode | 用途 | 输出 |
|---|---|---|
| `text` | 整页 OCR（最通用） | 逐页文本 + 行数 + 平均置信度 |
| `table` | **表格结构**提取（任意有数值列的表格） | **代码级路由**：文本层坐标 → 几何重建（列数自适应、不切碎数字）｜扫描件单栏宽表 → 几何重建｜扫描件 3 列窄表 / 双栏表 → 列切条重 OCR（已验证口径）→ 按 y 归行 → 数字清洗 → **一致性校验**（表内合计行自校验）；每页带 `route` 字段（决策 + 依据，可审计） |
| `flash`/`standard`/`deep` | 扫描件质量档（MinerU 4.0 分档思想）：flash=dpi120 快筛、standard=默认、deep=dpi300 关键页 | `doc_preprocess(tier=…)` |
| `clause` | **条款**关键词扫描（合同/协议） | 回购/对赌/反稀释/优先清算… 关键词命中 + 页码 + 引用片段 |
| `legal` | **法律类文档**关注点扫描 | 陈述保证/违约/争议解决/期限窗口/程序审批/交叉引用/签署授权/合规资质… |
| `strategy` | **战略/行业报告**要素扫描 | 市场规模/增长预测/竞争格局/客户订单/技术研发/里程碑/风险/政策… |

## 二、为什么自己写引擎（而不是直接调通用 OCR）

通用 OCR（含 PaddleOCR 默认流程）在**表格类**文档上会踩四个坑，这些坑是**通用**的，与行业无关：

1. **无框线三线表**：整页 OCR 会把相邻列合并、数字右对齐截断 —— 实测**漏识约 1/3 数字**；
2. **数字切碎**：按固定列数切条时，切条边界落进数字中间（`12,345` → `52.0` + `163`）；
3. **逗号/句点混淆**：`12,345` 被认成 `12.345`，需按千分位规则归一；
4. **无法自证**：OCR 数字不可全信，必须有一条**确定性**的自校验（表内合计行相等）才敢用。

本插件把上面几条沉淀成引擎能力：

- `table` 模式用「数字 token 的右缘聚类」自动定位数值列，再按**几何重建**（列数自适应）或**列切条重 OCR** 两条路取数，二者由代码级路由选择；
- born-digital PDF 直接用**文本层词级坐标**（比 OCR 更准、**零 OCR 成本**），不重复 OCR；
- `clean_number` 统一处理千分位/括号负数/逗号句点混淆；
- 一致性校验做**锚定精确匹配**（`^合计$` 这类整词匹配），避免「流动资产合计」被「资产合计」子串误配（实测踩过）。

## 三、运行环境（重要）

RapidOCR 依赖 `onnxruntime / opencv / pypdfium2` 等 **C 扩展 wheel**。DSH 内置 python 原本因 macOS 代码签名（hardened runtime + Library Validation）拒绝加载它们：

```text
ImportError: dlopen(.../cv2/....so): code signature not valid for use in process:
mapping process and mapped file (non-platform) have different Team IDs
```

**已修复**：用 `dsh-macos-signfix` 插件对内置 python 注入 `com.apple.security.cs.disable-library-validation` entitlement 并 ad-hoc 重签，现在内置 python 可以 `import onnxruntime, cv2, pypdfium2`（验收见 `test/macos-sign-report.txt`）。

`runner.js` 的解释器选择顺序：

1. `DSH_OCR_PYTHON` 环境变量（显式指定，最优先）；
2. **DSH 内置 python**（已做签名修复，首选）；
3. `/opt/anaconda3/bin/python3`（兜底；若 DSH 升级后内置 python 又被重签回原状，会自动降级到这里，并在返回里标 `fallback_used`）。

对非 DSH 解释器，`runner.js` 把 DSH site-packages（RapidOCR 装在这里）经 `core/_boot/sitecustomize.py` **追加**到 `sys.path` 末尾——Anaconda 自己的 numpy 优先、DSH 只补缺失包，避免 ABI 冲突。这与宿主提供的 `toolchain/v3py` 是同一套机制。

> 本机已验证：`findOcrPythonCandidates()` → `[dsh-builtin, anaconda]`，实际用 `dsh-builtin` 跑通，`fallback_used=null`（见 test/run_tests.py）。

### macOS 代码签名（根因 / 修复 / 验证）

- **根因**：DSH Desktop 的内置 python 是 Developer ID 签名（Team `NAN929V4UM`）且启用了 Hardened Runtime（`flags=0x10000(runtime)`）；Library Validation 只接受与进程同 Team ID 的动态库，而 pip wheel 的 `.so/.dylib` 是 ad-hoc（无 Team ID）→ dyld 拒绝。
- **修复**（`dsh-macos-signfix` 的 `doc_ocr_sign_fix(mode="auto")`）：给解释器注入 `disable-library-validation` entitlement，`codesign --force --options runtime --entitlements … --sign -` 重签；原二进制备份到 `dsh-ocr/sign/backup/<timestamp>/`。
- **验证**：
  ```bash
  DSH_PY=~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3
  codesign -dvvv --entitlements - "$DSH_PY" | grep disable-library
  "$DSH_PY" -c "import onnxruntime, cv2, pypdfium2; print('OK')"
  ```
- **已知限制**：每次 DSH Desktop 升级/重装运行时时，内置 python 可能被还原成原签名，需重跑一次 `doc_ocr_sign_fix(mode="auto")`；`doc_ocr` 在修复失效时会自动降级到 Anaconda 并在返回里标注 `fallback_used`。

## 四、安装

```bash
dsh plugin --profile <profile> add link:/abs/path/to/dsh-ocr
```

重启 DSH 后，新会话工具列表出现 `doc_ocr`。或在 Harness 内用插件管理安装本目录（`install_bundle`，target 指向本插件根目录）。

## 五、运行（引擎直跑，不经过 DSH）

```bash
# 内置 python（签名修复后即可直接跑）；换机时用 ./toolchain/v3py 或 Anaconda
PY=~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3
PLUGIN=/abs/path/to/dsh-ocr

# text（整页文本）
"$PY" "$PLUGIN/core/ocr.py" --input /path/to/doc.pdf --mode text --pages 1-3 --dpi 200

# table（表格页：建议 dpi 300）
"$PY" "$PLUGIN/core/ocr.py" --input /path/to/doc.pdf \
  --mode table --pages 6-7 --dpi 300 --out-json /tmp/tables.json --out-md /tmp/tables.md

# clause（条款）
"$PY" "$PLUGIN/core/ocr.py" --input /path/to/contract.png --mode clause
```

## 六、测试

`test/run_tests.py` 覆盖三类用例（**全部用真实文档做回归哨兵**）：

1. **fixture（合成图）**：验证引擎能读一张已知文本的图，断言关键词命中；
2. **条款扫描**：真实合同页面 → 断言条款关键词命中、且关键日期出现在文本里；
3. **真实扫描件表格**：某双栏扫描表格页 → 断言 `table` 模式抽出各合计行，且 **两期一致性校验 3/3 通过**（diff=0）。

> 第 3 类是**回归哨兵**：任何改动只要让这张表的校验掉线，就说明路由或分栏判据被改坏了。

```bash
PLUGIN=/abs/path/to/dsh-ocr
cd "$PLUGIN"
PYTHONPATH="$PLUGIN/core/_boot" \
V3_EXTRA_SITE="$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/lib/python3.12/site-packages" \
~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3 test/run_tests.py
```

## 七、并发控制（重要）

每次 `doc_ocr` 调用都会 **spawn 一个独立 Python 进程**并重新加载 RapidOCR 模型。并发调用时如果不限流，N 个进程 × onnxruntime 默认「满核线程」= CPU 严重超卖，单次调用被拖到 50s+，看起来像「一条线程卡住」。本插件做了两层限制：

| 层级 | 机制 | 默认 | 覆盖 |
|---|---|---|---|
| 进程内 | `core/ocr.py` 设置 `intra_op_num_threads` / `inter_op_num_threads`（级联 Det/Cls/Rec） | **2** | `DSH_OCR_THREADS`（设 0/负数表示不限、走默认满核） |
| 权重档 | `DSH_OCR_MODEL=v4\|v5\|v6`：**v6=PP-OCRv6**（默认，50 语种统一，rapidocr 3.10.0）· v4=PP-OCRv4（回滚档，rapidocr-onnxruntime 1.4.4） | **v6** | `DSH_OCR_MODEL`；v4/v6 在真值集上结果一致（15/15，表格校验 3/3） |
| 进程间 | `runner.js` 模块级 FIFO 信号量，限制同时运行的 OCR 进程数 | **2** | `DSH_OCR_MAX_CONCURRENT` |

实测（text 单页 / table p37）：

| 场景 | 修复前 | 修复后 |
|---|---|---|
| text 并发 2 | 2.9s | 2.3s |
| text 并发 8 | 13.9s | 5.3s |
| text 并发 16 | 26.5s | 11.9s |
| table 并发 4 | 53.6s | 23.6s |

> 注意：信号量是**同一 DSH host 进程内**的并发上限；跨 host 进程的多会话仍会各自 spawn 进程。若需跨进程限制，用 `DSH_OCR_MAX_CONCURRENT=1` + 外部排队，或按 `<workspace>/skills` 的批量流程串行调用。

## 八、知识库文件预处理（doc_preprocess）

把 7 阶段预处理流程整合为一个工具，用于「目录/批量原始文件 → Obsidian 可读知识库」：

```text
discover → classify → extract(后端路由) → clean → structure → output(md+json) → verify
              ↑                                                    ↓
           增量/缓存 ←────────────── 失败人工队列
```

| 阶段 | 做什么 |
|---|---|
| 0 discover | glob 扫描 + 排除（.git/.trash/node_modules 等），记 sha256 |
| 1 classify | 按扩展名 + PDF 文本层检测（>100 字符=文本层，≈0=扫描件）分类 |
| 2 extract | 文本层 PDF→pypdf；扫描件 PDF/图片→RapidOCR；docx→python-docx；xlsx→openpyxl；pptx→python-pptx；csv→markdown 表；json/yaml→代码块 |
| 3 clean | 页眉页脚去重、中文断行合并、数字清洗 |
| 4 structure | 第X章/第X条→标题层级、表格→markdown 表 |
| 5 output | 每文件 `.md`（YAML frontmatter：source_path/sha256/kind/backend/page_count/ocr_confidence/extracted_at）+ `.json` + `cache.json` |
| 6 verify | 空正文/无页/不支持格式 → 进 problems |
| 7 incremental | sha256 命中缓存 → skip（`force` 可重跑） |

用法（引擎直跑）：

```bash
python3 core/pipeline.py --input 某目录 --output out/ --mode all
python3 core/pipeline.py --input 某目录 --output out/ --mode classify   # 只分类
```

工具签名：

```text
doc_preprocess(path, outputDir?, mode=all|classify|discover, dpi?, maxFiles?, force?)
```

> 设计来源见 `research/obsidian-preprocessing/研究报告.md`（obsidian-import 后端路由、foldmark 懒 OCR、mineru 场景矩阵、text-extractor 缓存、vaultkeeper 附件治理）。

## 九、文档解析参考：paddleocr-doc-parsing

本插件的 `table` 模式是「表格结构识别」的**轻量实现**（x 聚类定列 + 列切条）。当它不够用——复杂表格结构、多栏版式、单元格跨行跨列（rowspan/colspan）——升级路径是 PaddleOCR 的文档解析流水线：

- **PaddleOCR PP-Structure**（文档分析 / 版面分析 + 表格结构识别）：[PP-Structure 文档分析](https://www.paddleocr.ai/latest/version2.x/ppstructure/overview.html) · [PP-StructureV3 通用版面解析产线](https://paddlepaddle.github.io/PaddleX/3.0-rc/pipeline_usage/tutorials/ocr_pipelines/PP-StructureV3.html)
- 它先用**版面分析模型**把页面切成「文本 / 表格 / 图」，再用**表格结构识别模型**给出每个单元格的坐标与跨行跨列关系，最后 OCR —— 这正是本插件 `table` 模式目前靠「列切条 + y 归行」近似做的事。

为什么当前**不直接装 PaddleOCR / paddlepaddle**：paddlepaddle 数百 MB、且本任务量级（几十页扫描件）用 RapidOCR 的 ONNX 推理已够（本插件的选型结论：paddleocr 过重，RapidOCR 已覆盖」）。若后续要处理复杂表格，按上述链接补装 PP-Structure，把本插件的 `table` 模式换成「版面分析 + 表格结构识别」即可，接口不变。

RapidOCR 本身就是 PaddleOCR PP-OCRv4 的 ONNX 运行时版（同检测/识别模型），所以两者模型是同源的——升级是「换解析层」而非「换识别引擎」。


## 十、排障：工具消失 / 引擎不可用

**症状**：会话里 `doc_ocr` / `doc_preprocess` 不见了，但 `doc_ocr_sign_fix` 还在。

**头号原因**：`dsh-ocr` 在 `~/.dsh/profiles/desktop/package.json` 的 `dependencies` 里，却不在 `dsh.profile.bundles` 里（插件装了没挂载）。触发路径：禁用→启用重载流、或 profile 重建时条目丢失。

**处置**：
```bash
# 1) 体检（18 项：签名/依赖/注册/冒烟）
python3 $PLUGIN/test/health_check.py
# 2) 若 D 项红：先移除再安装，然后重启 DSH
#    plugin_manager remove_bundle(target="dsh-ocr")
#    plugin_manager install_bundle(target="<插件目录>")
```

**其他常见原因**：
- 签名失效（内置 Python 被 DSH 升级还原）→ `doc_ocr_sign_fix(mode="auto")`；
- 跨 host 多会话并发 → 设 `DSH_OCR_MAX_CONCURRENT=1`。

## 十一、边界（明确不做）

- 不处理加密 PDF / 需要密码的文档（交给下游）；
- 不做定时任务、不做目录监控（那是调用方流水线的事）；
- 不编造数字：OCR 拿不到或自校验不平的数，标出来交人工，不硬凑；
- 有文本层的 PDF 优先**直读文本层**（pymupdf，零 OCR 成本、字符更准）；扫描件/图片才走 OCR；
- 不假设文档业务类型（领域无关：只产出结构，不产出业务结论）。

## 十二·五、OCR 结果正确性评分（Jev，可选）

`core/jev.py` 接入 [MiniCPM-OCR Jev](https://github.com/OpenBMB/MiniCPM-OCR)（2B，Apache-2.0，Qwen3.5 骨干 + 二分类头）：给「引擎读出的数字/文本」打 0–1 正确性分，用于自动接受高置信区域 / 把可疑区域路由人工复核 / 多引擎择优。

```bash
PY=~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3
"$PY" plugins/dsh-ocr/core/jev.py --input 页面.png --category table
```

- **与一致性校验互补**：表内校验（balance_check）是确定性规则、零成本、可复算，管「数字对不对」；Jev 管「这串字读对了吗」。二者都要，不互相替代。
- **依赖未就绪时降级**：权重缺失/无网络 → 返回 `jev_unavailable` 结构化错误，不阻塞既有流程（本机 huggingface.co 不可达，需 `HF_ENDPOINT=https://hf-mirror.com`）。

## 十二、证据索引（workpaper.py · 带地址引用的 RAG chunk）

把 OCR/管线 JSON 变成「结论 ↔ 证据一对一」的带地址索引（**通用的可溯源证据链**，不绑定任何行业）：

```bash
python3 core/workpaper.py --input <json目录或文件> --output <索引目录>
# 产物：workpaper_index.jsonl（RAG chunk 源）+ workpaper_index.md（人工复核）
```

地址协议：`<doc_id>:P<页>` 文本页 · `<doc_id>:P<页>T<t>R<r>` 扫描件表格行 · `<doc_id>:T<t>R<r>` Office 表格行 · `<doc_id>:P<页>C<n>` 条款命中（`doc_id`=源文件 sha256 前 8 位）。

纪律：每条结论必须引用 address；索引里没有的证据只能标 missing 待查，不编造。

> 领域化的用法（哪些证据该被谁消费、怎么裁决）写在**调用方的技能**里，不在本插件。

## 十二·六、性能与加速（2026-10-29 实测 · 零成本）

环境：Apple M2 Pro（12 核，arm64）· onnxruntime 1.31.1 · rapidocr 3.10.0（PP-OCRv6）· pypdfium2 5.14.0 · 插件 **0.6.0**

| 项 | 配置 | 实测 | 说明 |
|---|---|---|---|
| OCR 主链路 | `intra_op_num_threads=6` / `inter_op_num_threads=1` | **2.65s/页**（原 2 线程 6.31s，**2.4×**） | 输出完全一致（297 行 / conf 0.9940） |
| 并发保护 | runner 传 `DSH_OCR_CONCURRENCY` | 单跑 6 线程；并发 ≥2 → 2 线程/进程 | 防 CPU 超卖（原代码硬编码 2，一刀切太保守） |
| CoreML EP | `use_coreml: true` | **7.56s（更慢）** | ❌ 不采用：PP-OCRv6 动态 shape 无法静态绑 ANE，E5RT 报 unbounded dimension |
| Jev | `DSH_JEV_DEVICE=auto` → **MPS** | 整页 **19s**（原 ~20 分钟，**~60×**）；小区域 1.1s | 判别力不变（好 0.73 / 坏 0.0001） |
| Jev 用法 | `--regions` 先裁表格区段 | 整页喂 good/bad 都塌到 0.002；区域裁剪 0.73 / 0.0001 | Jev 是**区域级**模型 |

**装不了的算子库**（勿再尝试）：`causal-conv1d`（macOS arm64 无法构建，CUDA 专用）、
`flash-linear-attention`（`fla-core[cpu]` 依赖 triton>=3.3，macOS 无 wheel）、
`mlx-lm`（Jev 无生成头，不适用）。

环境变量：`DSH_OCR_THREADS`（强制线程数）、`DSH_OCR_CONCURRENCY`（当前并发数，由 runner 传）、
`DSH_JEV_DEVICE`（auto/cpu/mps）、`DSH_JEV_MAX_SIDE`（Jev 下采样边长）。

详见「性能与加速」一节的实测数据。

---

## 0.5.0 变更记录（2026-11-15）

**架构级**：从「切条重 OCR 单一路径」→「**代码级路由**的三路径」。

### 新增
| 能力 | 说明 | 验收 |
|---|---|---|
| **几何重建**（`core/geo_table.py`） | 用 OCR/文本层的**精确 bbox** 直接重建表格，列数**自适应**；**不再切图重 OCR** ⇒ 数字永不被切碎 | 两期一致性校验 diff=0.00；5 列明细表 `1203456.78` 完整（合成示例）|
| **文本层坐标**（调用方可用 pymupdf 自行取，或用本插件 `text_layer_bbox` 约定） | born-digital PDF 用 **pymupdf 词级 bbox**（比 OCR 更准、**零 OCR 成本**） | born-digital 40 页 100% 覆盖；表格页 **0 次引擎调用** |
| **代码级路由**（`core/ocr.py:table_extract`） | 决策输入全是**测量值**（坐标来源 / 栏数 / 列数），每页产出 `route{decision,reason,...}` | 双栏表 → slice（校验 3/3）；单栏宽表 → geometry |
| **分栏判据唯一实现**（`core/geo_table.detect_panels`） | 「最大间隙处切分且两侧各 ≥2 个列簇」；旧判据（标签间隙）既漏判又误判 | 5 个样本全判对 |
| **页型分类**（`geo_table.detect_panels` 的分栏判据） | `table/text/born_digital/empty` 由调用方自行判定；插件只提供**分栏判据**与可断言的 evidence | 纯函数，可离线测 |

### 修复
- **切条法列数硬编码 3** ⇒ ≥4 列表的数字被从中间切开（`1203456.78`→`12034.0`+`56.78`，合成示例）
- born-digital PDF 因 `skip_text_layer` 默认开启而**不产生任何坐标** ⇒ 表格结构能力为零
- 文本层页的 legacy 渲染被 C3 意外改变（破坏「逐字不变」硬线）
- 文本层页的精确坐标被 table 路径回写的 OCR 行**覆盖**（缓存污染）
- `scan_pages` 探测失败时 `sinfo["text_pages"]` **KeyError**（整个解析崩掉）
- 依赖 `pypdf`（DSH 运行时重建后已不存在）⇒ 文本层探测改用 **pymupdf 优先**

### 回归基线（本次全绿）
`真值集 15/15`（含双栏表格校验 3/3）· `体检 19/19` ·
扫描件表格页 `| 格子 | 格子 |` 行**逐字不变**（45/45）

---

## 0.6.0 变更记录（2026-11-23）

**定位变更：领域无关（domain-agnostic）的通用文档扫描插件。**

| 项 | 0.5.0 | 0.6.0 |
|---|---|---|
| 定位 | 「OCR 文档识别插件」，系统性提示把它绑在特定业务链路上 | **通用文档扫描识别插件**：PDF/图片 → 结构，**不假设文档业务类型** |
| 系统提示段 | 写满具体业务名词（某个行业的材料类型 + 下游链路名）| 只讲**能力与纪律**（何时用、怎么判、边界在哪）|
| 领域化用法 | 混在插件提示里 | **移到调用方自己的技能/流水线**（本插件不再携带）|
| 引擎表述 | PP-OCRv4（过时） | **PP-OCRv6** |
| 注释/文档 | 大量具体项目名 | 通用描述（**保留全部数值证据与技术事实**）|

> 为什么这么改：插件是**能力层**，业务是**使用层**。两件混在一起时，
> 插件无法在别的项目复用，而业务变更还要回来改插件的系统提示。
> 现在：插件只有能力，业务用法在技能里。

---

## 0.6.1 变更记录（2026-11-24）

**发布脱敏（对应工作区原则「插件发布原则-v1」）：插件里不得出现任何真实材料的名称、内容、数字、主体、人名、路径。**

| 项 | 改动 |
|---|---|
| 材料主体/人名 | 注释与文档里的具体公司名、人名 → 「某机构」「某股东甲」 |
| 材料金额 | 所有真实金额 → **合成示例**（并在行内标注「合成示例」）|
| 材料文档代号 | 内部文档编号 → 「某表格页」「某 2 列宽表」 |
| 本机路径 | 安装/运行示例的本机绝对路径 → `/abs/path/to/...` |
| 本机诊断产物 | 删除 `test/macos-sign-report.txt`（含本机路径与用户名）|

**示例数字原则**：一律用**明显合成**的数（如 `12,034,567.89`、`1,000 / 900`），并标注「合成示例」。
理由：真实金额哪怕只出现一个，也可能反查到具体材料主体；而机制说明用合成数效果完全一样。

> 自查方法：把每条「实测」描述给一个外部读者看 —— 他能否据此定位到某个具体材料或主体？能则必须脱敏。
