---
name: pdf-handwriting-to-word
version: "1.0.0"
description: "转写手写扫描版 PDF 并生成 Word 文档。当用户要求「转写/誊录手写扫描 PDF」「把手写稿变成 Word」「扫描件转文字」时使用。流程：PDF 渲染为 PNG → 逐页全分辨率辨读 → 按约定写成中间文本 → 一键生成带中文公文格式的 docx。"
platforms: [windows, macos, linux]
metadata:
  category: document-processing
  tags:
    - pdf
    - handwriting
    - ocr
    - docx
    - transcription
argument-hint: [pdf-file-path]
allowed-tools: Read, Write, Bash, ReadMediaFile, Agent, AgentSwarm
---

# 手写扫描 PDF 转写为 Word

把手写扫描件 PDF 转写为规范文字，并生成带中文公文排版（黑体标题、仿宋正文、首行缩进、1.5 倍行距）的 Word 文档。

## 环境与依赖

- 工作目录下已有 `.venv`（含 pymupdf）；如缺失：`python -m venv .venv && .venv/Scripts/python -m pip install pymupdf`
- 系统 python 需有 `python-docx`（`python -c "import docx"` 验证；缺失则在 `.venv` 中安装并用 `.venv/Scripts/python` 运行脚本）

## 流程

### 1. PDF 转图片

```bash
.venv/Scripts/python .agents/skills/pdf-handwriting-to-word/scripts/pdf_to_images.py "<pdf路径>" pdf_pages 200
```

输出 `pdf_pages/page_01.png …`（默认 200 dpi，A4 约 1653x2339）。

### 2. 逐页转写（最关键，省 token 要点）

- 先用 ReadMediaFile 整页看一次了解布局（图片会被降采样，草书细节看不清是正常的）。
- **必须用 `region` 参数分块看全分辨率**：每块宽约整页、高 500-600 原图像素，从上到下 4-5 块，块间留少量重叠防止漏行。
- 页数 ≥3 时，用 AgentSwarm 每页派一个子代理并行转写（提示词要求：分块全分辨率辨读、忠实原文、涂改按最终意图整理、无法辨认用〔？〕标注、忽略扫描水印）。主代理随后对存疑处（跨页断句、序号、人名）自行用 region 复核。
- 转写规则：
  - 划掉的内容删除；行间插入的文字并入正文；作者打勾确认的改动保留。
  - 跨页断句要衔接成完整句子。
  - 章节/条目序号按作者最终意图统一（如手稿删改了编号）。
  - 扫描水印（如"扫描全能王 创建"）不转写。
  - 把握不高的字词直接写出较可能的读音/字形，最后在交付说明里向用户列出待核对清单，不要留在正文里标注。

### 3. 写中间文本

把转写结果写成一个 UTF-8 文本文件（如 `转写稿.txt`），行首标记约定：

- `# ` 文档标题（可多行，居中黑体 16pt）
- `## ` 节标题（黑体 14pt）
- `### ` 条目标题（加粗仿宋）
- 其余行为正文段落（仿宋 12pt、首行缩进 2 字符、1.5 倍行距）

### 4. 生成 Word

```bash
python .agents/skills/pdf-handwriting-to-word/scripts/make_docx.py 转写稿.txt "输出文件名.docx"
```

### 5. 验证与交付

- 用 python-docx 读回 docx 确认段落数与首尾内容。
- 交付时向用户说明：涂改的处理方式、跨页衔接处、以及需要对照原件核对的存疑字词清单。

## 文件说明

- `scripts/pdf_to_images.py` — PDF 逐页渲染为 PNG（参数：pdf路径、输出目录、dpi）
- `scripts/make_docx.py` — 中间文本转 docx（参数：输入文本、输出 docx）；格式约定见脚本 docstring
