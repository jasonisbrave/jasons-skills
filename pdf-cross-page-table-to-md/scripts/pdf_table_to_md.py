# -*- coding: utf-8 -*-
"""用 pdfplumber 单元格级位置识别 PDF 跨页两栏表格并转为 Markdown。

方法来源：Learning/pdfplumber跨页表格转md方法.md
核心思路：
  1. find_tables() 定位每页表格与单元格边界
  2. extract_words() 提取文字块精确坐标
  3. 以单元格 bbox 为容器，按 x 坐标分左右列、按 y 坐标聚类行
  4. 合并同一单元格内因换行被拆开的条目
  5. 跨页顺序拼接，自动形成完整表格

用法:
    python pdf_table_to_md.py <pdf路径> <输出.md> [选项]

选项:
    --start-page N    表格起始页（1 起，默认 1）
    --end-page N      表格结束页（含，默认最后一页）
    --y-tol N         同一行内 y 坐标容差（默认 4，按字号 3-6 调整）
    --split-x N       左右列分割线 x 坐标（默认取表格垂直中线）
    --header "A,B"    Markdown 表头（默认 "考试内容,能力等级"）
    --no-entry-merge  不做换行条目合并（默认按 1. / 一、 / （ 识别新条目并合并）
    --filter KW       输出前剔除含关键词 KW 的行，可多次指定（用于过滤表后混入的正文）

示例:
    python pdf_table_to_md.py 大纲.pdf 大纲表格.md --start-page 1 --end-page 17 \
        --header "考试内容,能力等级" --filter 主要参考法规
"""
import argparse
import sys

import pdfplumber

NEW_ENTRY_PREFIXES = (
    ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "10.", "11."),
    ("一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、", "九、", "十、"),
    ("（",),
)


def is_new_entry(text):
    return any(text.startswith(p) for group in NEW_ENTRY_PREFIXES for p in group)


def extract_rows(pdf_path, start_page, end_page, y_tol, split_x, merge_entries):
    all_rows = []
    with pdfplumber.open(pdf_path) as pdf:
        end_page = min(end_page, len(pdf.pages))
        for page_idx in range(start_page - 1, end_page):
            page = pdf.pages[page_idx]
            tables = page.find_tables()
            if not tables:
                continue
            words = page.extract_words()

            for table in tables:
                bbox = table.bbox
                sx = split_x if split_x is not None else (bbox[0] + bbox[2]) / 2

                for row in table.rows:
                    # 当前单元格内的所有文字块
                    cell_words = [
                        w for w in words
                        if (row.bbox[0] - 1 <= w["x0"] <= row.bbox[2] + 1
                            and row.bbox[1] - 1 <= w["top"] <= row.bbox[3] + 1)
                    ]

                    # 按 y 坐标聚类成行
                    row_groups = []
                    for w in cell_words:
                        for rg in row_groups:
                            if abs(rg["top"] - w["top"]) <= y_tol:
                                rg["words"].append(w)
                                rg["top"] = sum(x["top"] for x in rg["words"]) / len(rg["words"])
                                break
                        else:
                            row_groups.append({"top": w["top"], "words": [w]})

                    # 按左右列拆分每一行
                    cell_lines = []
                    for rg in sorted(row_groups, key=lambda x: x["top"]):
                        rg["words"].sort(key=lambda x: x["x0"])
                        left, right = [], []
                        for w in rg["words"]:
                            (left if (w["x0"] + w["x1"]) / 2 < sx else right).append(w["text"])
                        content = "".join(left).strip()
                        level = "".join(right).strip()
                        if content:
                            cell_lines.append({"content": content, "level": level})

                    if not merge_entries:
                        all_rows.extend(cell_lines)
                        continue

                    # 合并同一单元格内因换行拆分的内容
                    merged = []
                    i = 0
                    while i < len(cell_lines):
                        content = cell_lines[i]["content"]
                        level = cell_lines[i]["level"]
                        while i + 1 < len(cell_lines) and not is_new_entry(cell_lines[i + 1]["content"]):
                            content += cell_lines[i + 1]["content"]
                            level = level or cell_lines[i + 1]["level"]
                            i += 1
                        merged.append({"content": content, "level": level})
                        i += 1
                    all_rows.extend(merged)
    return all_rows


def write_md(all_rows, output_path, header, filters):
    col_a, col_b = header
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"| {col_a} | {col_b} |\n|---|---|\n")

        # 跳过重复表头（如果有）
        start = 0
        for i, r in enumerate(all_rows):
            if r["content"] == col_a and r["level"] == col_b:
                start = i + 1
                break

        n = 0
        for r in all_rows[start:]:
            if any(kw in r["content"] for kw in filters):
                continue
            content = r["content"].replace("|", "\\|").replace("\n", " ")
            level = r["level"].replace("|", "\\|").replace("\n", " ")
            f.write(f"| {content} | {level} |\n")
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="PDF 跨页两栏表格转 Markdown（pdfplumber 单元格级坐标法）")
    ap.add_argument("pdf")
    ap.add_argument("output")
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--end-page", type=int, default=10**9)
    ap.add_argument("--y-tol", type=float, default=4)
    ap.add_argument("--split-x", type=float, default=None)
    ap.add_argument("--header", default="考试内容,能力等级")
    ap.add_argument("--no-entry-merge", action="store_true")
    ap.add_argument("--filter", action="append", default=[])
    args = ap.parse_args()

    header = [h.strip() for h in args.header.split(",", 1)]
    if len(header) != 2:
        print("--header 须为 \"左列名,右列名\"")
        sys.exit(1)

    rows = extract_rows(args.pdf, args.start_page, args.end_page,
                        args.y_tol, args.split_x, not args.no_entry_merge)
    print(f"提取行数: {len(rows)}")
    n = write_md(rows, args.output, header, args.filter)
    print(f"写入 {args.output}: {n} 行")
    if not rows:
        print("提示: 未找到表格，可调整页码范围，或改用 "
              "page.extract_tables(settings={'vertical_strategy':'text','horizontal_strategy':'text'}) 的无框线策略",
              file=sys.stderr)


if __name__ == "__main__":
    main()
