---
name: pdf-cross-page-table-to-md
version: "1.0.0"
description: "把 PDF 中跨多页的两栏表格（如「考试内容 | 能力等级」大纲表）精确转换为 Markdown 表格。当通用 PDF 转 Markdown 工具把跨页表格拆断、列对不齐、长条目换行丢失时使用。基于 pdfplumber 单元格级坐标识别（find_tables + extract_words，按 x 分列、按 y 聚类行），非 OCR。"
platforms: [windows, macos, linux]
metadata:
  category: document-processing
  tags:
    - pdf
    - pdfplumber
    - table
    - cross-page
    - markdown
argument-hint: [pdf-file-path]
allowed-tools: Read, Write, Bash, Edit
---

# PDF 跨页两栏表格转 Markdown

方法来源：`D:/Code/Learning/Learning/pdfplumber跨页表格转md方法.md`（含完整原理与对比说明）。

## 适用场景

- PDF 中存在跨多页的大表格，每页没有重复表头
- 普通 PDF 转 Markdown 工具按页解析，表格被拆断、列对不齐
- 表格为左右两栏布局，需精确保留两列对应关系（三列及以上见「局限」）

## 使用

依赖：系统 python 已有 `pdfplumber`（`python -c "import pdfplumber"` 验证；缺失则装到工作目录 `.venv`）。

```bash
python .agents/skills/pdf-cross-page-table-to-md/scripts/pdf_table_to_md.py \
    <pdf路径> <输出.md> \
    --start-page 1 --end-page 17 \
    --header "考试内容,能力等级" \
    --filter 主要参考法规
```

脚本会打印提取行数与写入行数；未找到表格时给出无框线策略提示。

## 关键参数

| 参数 | 含义 | 建议 |
|---|---|---|
| `--start-page` / `--end-page` | 表格所在页码范围（1 起，含端点） | 先翻 PDF 确认表格起止页，避免混入前后正文 |
| `--y-tol` | 同一行文字块 y 坐标容差 | 默认 4；按字号 3–6 调整 |
| `--split-x` | 左右列分割线 x 坐标 | 默认取表格垂直中线；列宽悬殊或跨栏异常时手动指定（如 300） |
| `--header` | Markdown 表头，逗号分隔 | 默认 `考试内容,能力等级` |
| `--no-entry-merge` | 关闭换行条目合并 | 默认按 `1.` / `一、` / `（` 识别新条目并合并同单元格换行 |
| `--filter` | 剔除含关键词的行，可多次 | 用于过滤表格后混入的正文（如「主要参考法规」） |

## 转换后必做检查

1. 对照原 PDF 抽查首、尾及跨页接缝处的若干行，确认两列对应关系正确。
2. 检查末尾是否混入表格之后的正文（用 `--filter` 剔除）。
3. 长条目换行是否被正确合并；若新条目前缀不在默认集合（`1.`、`一、`、`（`），需改脚本里的 `NEW_ENTRY_PREFIXES`。
4. 终端打印中文可能乱码（GBK 控制台），但写入的 UTF-8 文件内容正常——以文件为准。

## 局限与扩展

- **仅适用两列表格**。三列及以上：改用 `table.rows`/`table.cols` 获取每列 x 范围分别归类（修改脚本中按 `split_x` 二分的部分）。
- **无框线表格**：`find_tables()` 找不到时，改调 `extract_tables(settings={"vertical_strategy": "text", "horizontal_strategy": "text"})`。
- 极少数漏识别数字可结合 LLM 校对补全。
