/**
 * Host half of dsh-ocr.
 *
 * Registers one agent tool, `doc_ocr`, plus a short system-prompt section that
 * tells the agent when and how to use it. The tool deterministically turns a
 * PDF / image into structured content — plain page text, table rows with a
 * self-consistency check, or keyword hits for clause / legal / strategy scans.
 *
 * **Domain-agnostic by design**: this plugin is a general-purpose document
 * scanner. It never assumes what the document *is* (financial statement,
 * contract, invoice, paper, form). Business-specific downstream steps belong to
 * the caller's own skills/pipelines, not here.
 *
 * All heavy lifting lives in the bundled Python engine (core/ocr.py,
 * RapidOCR = PaddleOCR PP-OCRv6 ONNX); runner.js locates an OCR-capable Python
 * and normalizes its JSON.
 *
 * Keeps ZERO runtime npm dependencies: everything it needs is bundled under core/.
 *
 * @module dsh-ocr
 */
import { fileURLToPath } from 'node:url'
import { dirname, isAbsolute, resolve } from 'node:path'
import { runOcr, runPreprocess } from './runner.js'

/** Cordis plugin name. */
export const name = 'dsh-ocr'

/** Required host services: tool registry + prompt assembly. */
export const inject = ['tools', 'systemPrompt']

const HERE = dirname(fileURLToPath(import.meta.url))

/* ------------------------------------------------------------------ */
/* defineTool (structure-compatible reimplementation of the SDK's)     */
/* ------------------------------------------------------------------ */

function compileValue(spec) {
  const node = {}
  if (typeof spec.description === 'string' && spec.description.length > 0) node.description = spec.description
  const type = spec.type
  if (type === undefined || type === 'json') return node
  if (type === 'object') {
    node.type = 'object'
    node.additionalProperties = spec.additionalProperties
    if (spec.properties !== undefined) node.properties = compilePropertyMap(spec.properties).properties
    return node
  }
  if (type === 'array') {
    node.type = 'array'
    if (spec.items !== undefined) node.items = compileValue(spec.items)
    return node
  }
  node.type = type
  if (spec.enum !== undefined) node.enum = [...spec.enum]
  if (spec.const !== undefined) node.const = spec.const
  return node
}

function compilePropertyMap(spec) {
  const properties = {}
  const required = []
  for (const [key, entry] of Object.entries(spec)) {
    const { required: isRequired, ...valueSpec } = entry
    properties[key] = compileValue(valueSpec)
    if (isRequired === true) required.push(key)
  }
  return required.length > 0 ? { properties, required } : { properties }
}

function matchesScalarType(value, type) {
  switch (type) {
    case 'string': return typeof value === 'string'
    case 'number': return typeof value === 'number'
    case 'integer': return typeof value === 'number' && Number.isInteger(value)
    case 'boolean': return typeof value === 'boolean'
    case 'null': return value === null
    default: return true
  }
}

function validateValue(schema, value, path) {
  if (typeof schema.type !== 'string' || schema.type.length === 0) return []
  if (schema.type === 'object') {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return [`${path} must be an object`]
    const violations = []
    for (const key of schema.required ?? []) if (!(key in value)) violations.push(`${path}.${key} is required`)
    if (schema.additionalProperties === false) {
      const known = new Set(Object.keys(schema.properties ?? {}))
      for (const key of Object.keys(value)) if (!known.has(key)) violations.push(`${path}.${key} is not a declared property`)
    }
    for (const [key, child] of Object.entries(schema.properties ?? {})) {
      if (key in value) violations.push(...validateValue(child, value[key], `${path}.${key}`))
    }
    return violations
  }
  if (schema.type === 'array') {
    if (!Array.isArray(value)) return [`${path} must be an array`]
    const violations = []
    if (schema.items !== undefined) value.forEach((item, i) => violations.push(...validateValue(schema.items, item, `${path}[${i}]`)))
    return violations
  }
  if (!matchesScalarType(value, schema.type)) return [`${path} must be ${schema.type}`]
  if (schema.enum !== undefined && !schema.enum.some((v) => v === value)) return [`${path} must be one of ${schema.enum.map(String).join(', ')}`]
  if (schema.const !== undefined && schema.const !== value) return [`${path} must be ${String(schema.const)}`]
  return []
}

function defineTool(options) {
  const compiled = compilePropertyMap(options.parameters)
  const parameters = { type: 'object', properties: compiled.properties }
  if (compiled.required !== undefined) parameters.required = compiled.required
  return {
    name: options.name,
    description: options.description,
    parameters,
    output: {
      schema: {},
      render(args, value) {
        return options.output.render(args, value)
      },
    },
    async execute(args, exec) {
      const violations = validateValue(parameters, args, 'arguments')
      if (violations.length > 0) throw new Error(`Error: invalid arguments: ${violations.join('; ')}`)
      return options.execute(args, exec)
    },
  }
}

/* ------------------------------------------------------------------ */
/* rendering                                                           */
/* ------------------------------------------------------------------ */

function fmt(v) {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number' && Math.abs(v - Math.round(v)) < 1e-9) return String(Math.round(v))
  return String(v)
}

function renderOcr(_args, value) {
  if (value === null || typeof value !== 'object') return [{ type: 'text', text: String(value) }]
  if (value.error !== undefined || value.ok === false) {
    const detail = value.detail ? `\n${value.detail}` : ''
    const hint = value.hint ? `\n提示：${value.hint}` : ''
    return [{ type: 'text', text: `OCR 失败：${value.error}${detail}${hint}` }]
  }

  const lines = []
  const mode = value.mode ?? 'text'
  const head = `引擎 ${value.engine ?? ''} · ${mode} 模式 · ${value.page_count ?? 0} 页 · ${value.elapsed_s ?? 0}s`
  lines.push(`# OCR · ${value.input ?? ''}\n${head}`)

  for (const p of value.pages ?? []) {
    if (mode === 'table' && p.table?.ok) {
      const t = p.table
      lines.push(`\n## 第 ${p.page} 页（表 · ${t.columns} 列）`)
      const header = `| 项目 | ${Array.from({ length: t.columns }, (_, i) => `列${i}`).join(' | ')} |`
      lines.push(header, `|---|${'---|'.repeat(t.columns)}`)
      for (const r of t.rows ?? []) {
        lines.push(`| ${r.label} | ${r.cells.map(fmt).join(' | ')} |`)
      }
      for (const b of t.balance_checks ?? []) {
        lines.push(`> 勾稽 col${b.col}: 资产总计 ${fmt(b.assets_total)} vs 负债+权益 ${fmt(b.liab_plus_equity)} → ${b.balanced ? '✓ 平衡' : '✗ 差 ' + b.diff}`)
      }
    } else if (mode === 'clause' || mode === 'legal' || mode === 'strategy') {
      // clauses are emitted once, after the loop
    } else {
      lines.push(`\n## 第 ${p.page} 页（${p.lines} 行 · conf ${p.avg_conf}）`)
      lines.push('```')
      lines.push(p.text)
      lines.push('```')
    }
  }

  if ((mode === 'clause' || mode === 'legal' || mode === 'strategy') && Array.isArray(value.clauses) && value.clauses.length > 0) {
    lines.push('\n## 扫描命中')
    for (const c of value.clauses) {
      lines.push(`- **${c.clause_type}**（${c.page_count_hit} 页，首页 p${c.first_page}）：${(c.quotes?.[0] ?? '').slice(0, 120)}`)
    }
  }

  const meta = []
  if (value.report?.jsonPath) meta.push(`完整 JSON：${value.report.jsonPath}`)
  if (value.report?.mdPath) meta.push(`报告：${value.report.mdPath}`)
  if (meta.length > 0) lines.push('', meta.join(' · '))
  return [{ type: 'text', text: lines.join('\n') }]
}

function renderPreprocess(_args, value) {
  if (value === null || typeof value !== 'object') return [{ type: 'text', text: String(value) }]
  if (value.error !== undefined || value.ok === false) return [{ type: 'text', text: `预处理失败：${value.error}` }]

  const lines = [`# 知识库预处理 · ${value.mode ?? 'all'}`, `输入：${value.input ?? ''}`, `输出：${value.output ?? ''}`]
  lines.push('', `发现 ${value.discovered ?? 0} 个文件 · 处理 ${Array.isArray(value.processed) ? value.processed.length : 0} · 跳过 ${Array.isArray(value.skipped) ? value.skipped.length : 0} · 失败 ${Array.isArray(value.failed) ? value.failed.length : 0} · ${value.elapsed_s ?? 0}s`)

  if (Array.isArray(value.processed) && value.processed.length > 0) {
    lines.push('', '| 文件 | 类型 | 后端 | 页数 | 问题 |')
    lines.push('|---|---|---|---|---|')
    for (const p of value.processed.slice(0, 30)) {
      const name = String(p.path ?? '').split('/').pop()
      lines.push(`| ${name} | ${p.kind ?? ''} | ${p.backend ?? ''} | ${p.pages ?? ''} | ${(p.problems ?? []).join(',') || '—'} |`)
    }
    if (value.processed.length > 30) lines.push(`| …其余 ${value.processed.length - 30} 个 | | | | |`)
  }
  if (Array.isArray(value.failed) && value.failed.length > 0) {
    lines.push('', '## 失败')
    for (const f of value.failed) lines.push(`- ${f.path}: ${f.error}`)
  }
  const meta = []
  if (value.interpreter) meta.push(`解释器：${value.interpreter}`)
  if (meta.length > 0) lines.push('', meta.join(' · '))
  return [{ type: 'text', text: lines.join('\n') }]
}

/* ------------------------------------------------------------------ */
/* plugin apply                                                        */
/* ------------------------------------------------------------------ */

const PROMPT_SECTION = `## 文档扫描识别（doc_ocr）

把**图片或扫描件 PDF（无文本层）**转成可被下游消费的结构化内容。通用文档扫描工具：不绑定任何业务领域，财务表、合同、票据、表单、论文、报告一视同仁。

- 参数 path：文件路径（.pdf / .png / .jpg / .bmp / .webp / .tif）；PDF 逐页渲染成图后识别。
- 参数 mode：\`text\`（整页文本，最通用）/ \`table\`（**表格结构**：数值列聚类 → 几何重建或切条重 OCR → 数字清洗 → 一致性校验）/ \`clause\`（条款关键词命中）/ \`legal\`（法律类文档关注点扫描）/ \`strategy\`（战略/行业类文档要素扫描）。
- 参数 pages：页范围 "all" / "1-10" / "34,37,42"；dpi：text 默认 200，table 建议 300–400。
- 引擎：RapidOCR（PaddleOCR PP-OCRv6 ONNX，本地推理、零联网、零 API 费用）。多语言：ch / en。
- 输出：逐页文本 + 平均置信度 + JSON/Markdown 报告路径；\`table\` 模式额外给表格行、列名与一致性校验结果。
- 每页可带 \`route\` 字段（走了哪条抽取路径 + 依据），用于复核。

使用纪律：
1. **先判有没有文本层**：有文本层的 PDF 优先直读（pymupdf，零 OCR 成本、字符更准），不要无谓 OCR；扫描件/图片才走 OCR。
2. \`table\` 模式只给「能自校验」的数：校验不平的标出来交人工核对，**不硬凑、不猜**。
3. OCR 数字有逗号/句点混淆（52,163 vs 52.163），按千分位规则归一，并保留原始文本可复核。
4. 关键词类模式（clause / legal / strategy）只给「类型 + 页码 + 引用」，不照抄长段落。**命中 ≠ 结论**：命中只表示「此处提到」；命中为 0 时如实说「未命中」，不要反推「不存在」。
5. **能力边界要如实说**：手写体、印章覆盖、严重倾斜、低分辨率扫描的识别率会明显下降；表格跨页、合并单元格、无框线嵌套表属于难例，产出须标注置信度供人工复核。
6. **零联网**：推理全在本地 ONNX，不产生搜索/API 费用；本插件不做任何联网调用。
7. 详细用法、档位与加速参数见插件 README；领域化的下游流程不在本工具内，由调用方的技能/流水线自行约定。

## 知识库文件预处理（doc_preprocess）

当要把一个目录或一批原始文件（PDF/Office/图片/文本）批量整理成 Obsidian 可读的知识库（markdown + JSON）时，用 doc_preprocess 跑 7 阶段管线：
discover → classify → extract → clean → structure → output → verify（含 sha256 增量跳过）。

- 参数 path：文件或目录；outputDir：输出目录（默认 <input>/.preprocessed）。
- 后端路由：文本层 PDF → pymupdf；扫描件 PDF/图片 → RapidOCR；docx → python-docx；xlsx → openpyxl；pptx → python-pptx；csv → markdown 表；json/yaml → 代码块。
- 输出：每文件一个 .md（YAML frontmatter：source_path/sha256/kind/backend/page_count/ocr_confidence/extracted_at）+ .json + cache.json。
- 模式：all（默认）/ classify（只分类）/ discover（只列文件）；force 忽略缓存重跑。
- 纪律：原始文件不改；markdown 是工作格式，结构化数值另存 JSON；失败进 failed 列表交人工。`

function resolveInputPath(args, exec) {
  let p = args.path
  if (typeof p !== 'string' || p.trim().length === 0) throw new Error('Error: invalid_input: path is required')
  if (!isAbsolute(p)) {
    const cwd = exec?.agent?.session?.header?.cwd
    if (typeof cwd !== 'string' || cwd.length === 0) throw new Error('Error: invalid_input: relative path given but the session has no working directory')
    p = resolve(cwd, p)
  }
  return p
}

export function apply(ctx) {
  const disposers = []

  disposers.push(ctx.tools.register(defineTool({
    name: 'doc_ocr',
    description: 'Scan a PDF or image into structured content (general-purpose document scanning; not tied to any business domain). Modes: text (per-page text + confidence), table (table structure: numeric-column clustering, geometric reconstruction or column-slice re-OCR, number cleaning, self-consistency checks), clause (clause keyword hits), legal (legal-document concern scan), strategy (strategy/industry report keyword scan). All scan modes return hits with page + quote. Engine: RapidOCR running PaddleOCR PP-OCRv6 ONNX models, fully local (no network, no API fees). Read-only; never modifies the source. For advanced table-structure / layout parsing, the upgrade reference is PaddleOCR PP-Structure (paddleocr-doc-parsing).',
    parameters: {
      path: {
        type: 'string',
        required: true,
        description: 'Input file path (.pdf / .png / .jpg / .bmp / .webp / .tif). Absolute, or relative to the session working directory.',
      },
      mode: {
        type: 'string',
        enum: ['text', 'table', 'clause', 'legal', 'strategy'],
        description: 'Scan mode. text = whole-page text (default). table = table-structure extraction (numeric-column clustering + geometric reconstruction or column-slice re-OCR + self-consistency checks). clause = clause keyword scan. legal = legal-document concern scan (representations/breach/dispute-resolution/deadlines/approvals/signatures). strategy = strategy-report keyword scan (market size/growth/competition/customers/technology/milestones/risks).',
      },
      pages: {
        type: 'string',
        description: 'Page selection: "all" (default), a range "1-10", or a list "34,37,42". Images ignore this.',
      },
      dpi: {
        type: 'integer',
        description: 'Render DPI before OCR. Default 200 (text/clause), 300 (table). Higher DPI (300-400) improves small-number accuracy on dense numeric tables.',
      },
      lang: {
        type: 'string',
        enum: ['ch', 'en'],
        description: 'Recognition language: ch (default) or en.',
      },
      outputDir: {
        type: 'string',
        description: 'Directory for the JSON + Markdown reports (default: <input dir>/.ocr).',
      },
    },
    output: { render: renderOcr },
    async execute(args, exec) {
      const input = resolveInputPath(args, exec)
      const outputDir = typeof args.outputDir === 'string' && args.outputDir.trim().length > 0
        ? (isAbsolute(args.outputDir) ? args.outputDir : resolve(dirname(input), args.outputDir))
        : undefined
      return runOcr({
        input,
        mode: args.mode,
        pages: args.pages,
        dpi: args.dpi,
        lang: args.lang,
        outputDir,
        engineRoot: HERE,
      })
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'doc_preprocess',
    description: 'Preprocess a file or directory into Obsidian-ready Markdown + JSON through a 7-stage pipeline: discover → classify → extract (backend routing: pypdf for text PDFs, RapidOCR for scanned PDFs/images, python-docx/python-pptx/openpyxl for Office) → clean → structure → output (YAML frontmatter + JSON) → verify, with sha256-based incremental skip. Read-only on sources; writes .md/.json/cache.json under outputDir.',
    parameters: {
      path: {
        type: 'string',
        required: true,
        description: 'Input file or directory to preprocess. Absolute, or relative to the session working directory.',
      },
      outputDir: {
        type: 'string',
        description: 'Output directory for .md/.json/cache.json (default: <input>/.preprocessed).',
      },
      mode: {
        type: 'string',
        enum: ['all', 'classify', 'discover'],
        description: 'Pipeline mode. all = full 7 stages (default). classify = only report file kinds. discover = only list files.',
      },
      dpi: {
        type: 'integer',
        description: 'OCR render DPI for scanned PDFs/images (default 200).',
      },
      maxFiles: {
        type: 'integer',
        description: 'Cap the number of files processed in one run (default 200).',
      },
      tier: {
        type: 'string',
        enum: ['flash', 'standard', 'deep'],
        description: 'Scan quality tier (MinerU-style): flash = fast triage (dpi 120, text only), standard = default, deep = high-fidelity key pages (dpi 300).',
      },
      force: {
        type: 'boolean',
        description: 'Ignore the sha256 cache and reprocess every file (default false).',
      },
    },
    output: { render: renderPreprocess },
    async execute(args, exec) {
      const input = resolveInputPath(args, exec)
      const outputDir = typeof args.outputDir === 'string' && args.outputDir.trim().length > 0
        ? (isAbsolute(args.outputDir) ? args.outputDir : resolve(dirname(input), args.outputDir))
        : undefined
      return runPreprocess({
        input,
        outputDir,
        mode: args.mode,
        dpi: args.dpi,
        maxFiles: args.maxFiles,
        tier: args.tier,
        force: args.force === true,
        engineRoot: HERE,
      })
    },
  })))

  const disposeSection = ctx.systemPrompt.section({
    name: 'dsh-ocr',
    order: 187,
    text: PROMPT_SECTION,
  })
  ctx.effect(() => disposeSection, 'dsh-ocr: prompt section')

  ctx.effect(() => () => {
    for (const dispose of disposers.splice(0)) dispose()
  }, 'dsh-ocr: tools')
}
