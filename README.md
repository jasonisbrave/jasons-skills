# jasons-skills

个人维护的 AI 代理技能（Agent Skills）合集。每个技能是一个自包含目录，内含 `SKILL.md`（流程说明与约定）和可直接运行的脚本，供支持 skills 机制的 AI 编码代理（如 Kimi Code、opencode 等）加载复用。

## 技能列表

### pdf-handwriting-to-word

转写手写扫描版 PDF 并生成 Word 文档。

- PDF 逐页渲染为 PNG（PyMuPDF），逐页全分辨率辨读手写内容
- 页数较多时支持按页派子代理并行转写，控制上下文 token 消耗
- 约定涂改/插入/跨页断句/扫描水印的处理规则
- 转写稿（带 `#`/`##`/`###` 简单标记的文本）一键生成中文公文格式 docx（黑体标题、仿宋正文、首行缩进、1.5 倍行距，python-docx）

```bash
python scripts/pdf_to_images.py <pdf路径> pdf_pages 200
python scripts/make_docx.py 转写稿.txt 输出.docx
```

### pdf-cross-page-table-to-md

把 PDF 中跨多页的两栏表格（如「考试内容 | 能力等级」大纲表）精确转换为 Markdown 表格。

- 基于 pdfplumber 单元格级坐标识别（`find_tables()` + `extract_words()`，按 x 分列、按 y 聚类行），非 OCR
- 跨页自动拼接、表头只保留一次、同单元格换行条目自动合并
- 页码范围、表头、y 容差、列分割线、关键词过滤均命令行可调

```bash
python scripts/pdf_table_to_md.py <pdf路径> <输出.md> \
    --start-page 1 --end-page 17 --header "考试内容,能力等级" --filter 主要参考法规
```

### omk-slim（Kimi Code 插件，v1.1.0）

复刻 oh-my-opencode-slim 的多智能体编排与模型路由套件（omk = Oh My Kimi Code）：主 Agent 作为编排者，按场景把子任务派发给 `explorer`（代码侦察）/ `librarian`（外部调研）/ `fixer`（实现）/ `oracle`（架构顾问）四个专家子智能体，并配合 `[secondary_model]` 模型池把不同场景路由到不同档位的模型，平衡质量与成本。v1.1.0 起支持跨工具派发（A+C 模式）：廉价批量只读任务外派给 OpenCode，与 omz-slim 的编排纪律一致。

```bash
# 克隆后在 Kimi Code 中：
/plugins install <克隆路径>/omk-slim
/reload
```

详见 [omk-slim/README.md](omk-slim/README.md)（含模型池配置步骤）。

### omz-slim（ZCode 插件，v1.0.1）

参考 oh-my-opencode-slim 的成本控制思路为 ZCode 做的编排插件（omz = Oh My ZCode），v1.0.1 起升级为 ZCode / Kimi Code / OpenCode 三工具编排层：主对话作为编排者，把读代码、机械改动的重 token 工作派给后台子智能体（`omz-explorer` / `omz-fixer` 路由到 GLM-5.3-Flash，`omz-oracle` 路由到 GLM-5.3、仅手动触发），主上下文只保留规划与汇总；跨工具派发把廉价批量任务交给 OpenCode（`/oc`）、把交付级实现与评审交给 Kimi Code（`/kimi`）。共 5 个斜杠命令：`/delegate`、`/oracle`、`/preset`、`/kimi`、`/oc`。v1.0.2 起编排规则通过 SessionStart 钩子在**每个新会话默认注入**，无需手动调用技能。

```bash
# ZCode 中：Settings → Plugin Management → Discover → +
# 将本仓库（或含 omz-slim/ 的本地目录）添加为 marketplace，安装并启用 omz-slim
```

详见 [omz-slim/README.md](omz-slim/README.md)。

## 依赖

- Python 3.10+
- `pdf-handwriting-to-word`：`pymupdf`、`python-docx`
- `pdf-cross-page-table-to-md`：`pdfplumber`

## 安装方式

将对应技能目录复制到代理的 skills 目录（如项目级 `.agents/skills/` 或用户级 `~/.agents/skills/`）即可被自动发现。

## License

[MIT](LICENSE)
