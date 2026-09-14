#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告勾稽检查（check_type="报告勾稽"）。

用法:
    python check_links.py <outdir> --link-map <link_map.json> -o results.json

link_map.json 由调用方（Agent）生成，schema:
{"links": [{"id": "利润总额", "text_file": "报告.docx", "text_hint": "利润总额",
            "cell_file": "申报表.xlsx", "sheet": "A100000...主表", "coord": "E21",
            "tolerance": 0.01, "note": "..."}]}

对每条链接：在 report_text.md 中找 text_hint 所在段落 → 提取段落全部金额
（千分位、"万元"×10000、负数/括号负数）→ 取与表格缓存值最接近的一个比较
（Decimal，容差默认 0.01）。
"""

from __future__ import annotations

import argparse
import re
import sys
from decimal import Decimal
from pathlib import Path

# Windows 控制台输出防乱码（Python 3.7+）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _common import (TOLERANCE, compare_with_tolerance, dump_json, fmt_num,  # noqa: E402
                     load_json, make_result, match_file_key, parse_report_text,
                     setup_console, to_decimal)

CHECK_TYPE = "报告勾稽"
CONTEXT_LEN = 50  # 原文摘录长度

# 金额：[负号或括号] 数字 [括号] 元/万元
AMOUNT_RE = re.compile(
    r"(?P<sign>-\s?|−\s?)?\(?\s*(?P<inner_neg>-)?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*\)?\s*(?P<unit>万元|元)"
)
# 兜底裸数字（段落无带"元"金额时使用）
BARE_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def extract_amounts(text: str) -> list[Decimal]:
    """提取段落中的金额。优先带'元/万元'单位的；没有时退回裸数字（剔除 1900-2099 年份）。"""
    amounts: list[Decimal] = []
    for m in AMOUNT_RE.finditer(text):
        num = Decimal(m.group("num").replace(",", ""))
        if m.group("unit") == "万元":
            num = num * Decimal(10000)
        if m.group("sign") or m.group("inner_neg"):
            num = -num
        amounts.append(num)
    if amounts:
        return amounts
    for m in BARE_NUM_RE.finditer(text):
        num = Decimal(m.group().replace(",", ""))
        if num == num.to_integral_value() and Decimal(1900) <= num <= Decimal(2099):
            continue  # 大概率是年份
        amounts.append(num)
    return amounts


def check_link(link: dict, text_entries: list[dict], sheets_data: dict, results: list[dict]) -> None:
    link_id = str(link.get("id") or link.get("text_hint") or "?")
    text_file = str(link.get("text_file", ""))
    hint = str(link.get("text_hint", ""))
    sheet = str(link.get("sheet", ""))
    coord = str(link.get("coord", "")).replace("$", "").upper()
    tolerance = Decimal(str(link.get("tolerance", float(TOLERANCE))))
    note = str(link.get("note", ""))

    # ---- 定位文本段落 ----
    cand_files = {e["file"] for e in text_entries}
    matched_file = match_file_key(cand_files, text_file)
    hits = [e for e in text_entries
            if (matched_file is None or e["file"] == matched_file)
            and hint and hint in e["text"]]
    if not hits:
        results.append(make_result(
            CHECK_TYPE, "勾稽定位失败", "warning", False,
            f"[{link_id}] 在 report_text.md 中未找到包含“{hint}”的段落"
            + (f"（文件 {text_file}）" if text_file else ""),
            source_location=text_file or "",
            evidence=note,
        ))
        return
    # 优先取有金额的段落
    entry = next((e for e in hits if extract_amounts(e["text"])), hits[0])
    text_loc = f'{entry["file"]}#{entry["kind"]}={entry["idx"]}' + (
        f'!row={entry["row"]}' if entry["kind"] == "table" and entry.get("row") else "")

    # ---- 提取报告金额 ----
    amounts = extract_amounts(entry["text"])
    context = entry["text"].strip()[:CONTEXT_LEN]

    # ---- 定位表格单元格 ----
    cell_loc = ""
    cell_dec: Decimal | None = None
    matched_cell_file = match_file_key(sheets_data.keys(), str(link.get("cell_file", "")))
    if matched_cell_file is None:
        cell_loc = f"{link.get('cell_file', '?')}（未在解析结果中找到该文件）"
    else:
        sdata = sheets_data[matched_cell_file].get("sheets", {}).get(sheet)
        cell_loc = f"{matched_cell_file}#{sheet}!{coord}"
        if sdata is None:
            cell_loc += "（sheet 不存在）"
        else:
            centry = sdata.get("cells", {}).get(coord)
            v = centry.get("v") if centry else None
            if centry is None or v is None or v == "":
                results.append(make_result(
                    CHECK_TYPE, "单元格为空", "warning", False,
                    f"[{link_id}] {sheet}!{coord} 无缓存值（空或未计算）",
                    source_location=f"{text_loc} | {cell_loc}", context=context, evidence=note,
                ))
                return
            try:
                cell_dec = to_decimal(v)
            except Exception:
                results.append(make_result(
                    CHECK_TYPE, "单元格为空", "warning", False,
                    f"[{link_id}] {sheet}!{coord} 缓存值非数值: {v!r}",
                    source_location=f"{text_loc} | {cell_loc}", context=context, evidence=note,
                ))
                return

    # ---- 段中无数字 ----
    if not amounts:
        results.append(make_result(
            CHECK_TYPE, "报告缺数字", "warning", False,
            f"[{link_id}] 命中段落中未提取到金额数字",
            source_location=f"{text_loc} | {cell_loc}", context=context, evidence=note,
        ))
        return

    # ---- 取与表格值最接近的金额比较 ----
    target = cell_dec if cell_dec is not None else Decimal(0)
    report_amt = min(amounts, key=lambda a: abs(a - target))
    if cell_dec is None:
        results.append(make_result(
            CHECK_TYPE, "单元格为空", "warning", False,
            f"[{link_id}] 单元格无值，无法比较",
            expected=fmt_num(report_amt), source_location=f"{text_loc} | {cell_loc}",
            context=context, evidence=note,
        ))
        return

    ok, diff = compare_with_tolerance(report_amt, cell_dec, tolerance)
    source_location = f"{text_loc} | {cell_loc}"
    if ok:
        results.append(make_result(
            CHECK_TYPE, "勾稽相符", "info", True,
            f"[{link_id}] 报告数与表格数一致（{note}）" if note else f"[{link_id}] 报告数与表格数一致",
            expected=fmt_num(report_amt), actual=fmt_num(cell_dec), difference="0.00",
            source_location=source_location, context=context, evidence=note,
        ))
    else:
        results.append(make_result(
            CHECK_TYPE, "勾稽不一致", "error", False,
            f"[{link_id}] 报告数与表格数不一致（{note}）" if note else f"[{link_id}] 报告数与表格数不一致",
            expected=fmt_num(report_amt), actual=fmt_num(cell_dec), difference=fmt_num(diff),
            source_location=source_location, context=context, evidence=note,
        ))


def run(work_dir: str | Path, link_map_path: str | Path) -> list[dict]:
    """报告勾稽入口。work_dir 需含 report_text.md 与 sheets.json（至少其一）。"""
    work = Path(work_dir)
    link_map = load_json(link_map_path)
    links = link_map.get("links") if isinstance(link_map, dict) else link_map
    if not isinstance(links, list):
        raise ValueError("link_map.json 格式错误：应为 {\"links\": [...]}")

    md_path = work / "report_text.md"
    sheets_path = work / "sheets.json"
    if not md_path.exists() and not sheets_path.exists():
        raise FileNotFoundError(f"{work} 下缺少 report_text.md / sheets.json（先运行 parse_tax.py）")

    text_entries: list[dict] = []
    if md_path.exists():
        text_entries = parse_report_text(md_path.read_text(encoding="utf-8"))
    sheets_data: dict = {}
    if sheets_path.exists():
        sheets_data = load_json(sheets_path)

    results: list[dict] = []
    for link in links:
        try:
            check_link(link if isinstance(link, dict) else {}, text_entries, sheets_data, results)
        except Exception as e:  # 单条链接数据异常不中断
            results.append(make_result(
                CHECK_TYPE, "勾稽定位失败", "warning", False,
                f"链接项处理异常: {type(e).__name__}: {e}",
                source_location=str(link.get("id", "?")) if isinstance(link, dict) else "?",
            ))
    return results


def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(description="报告勾稽检查（报告段落金额 vs 申报表单元格）")
    parser.add_argument("workdir", help="parse_tax.py 的输出目录（含 report_text.md / sheets.json）")
    parser.add_argument("--link-map", required=True, help="勾稽链接定义 link_map.json")
    parser.add_argument("-o", "--out", required=True, help="输出 results.json 路径")
    args = parser.parse_args()
    try:
        results = run(args.workdir, args.link_map)
        dump_json(args.out, results)
        errors = sum(1 for r in results if r["severity"] == "error")
        warns = sum(1 for r in results if r["severity"] == "warning")
        print(f"[check_links] 完成：{len(results)} 项结果（error {errors} / warning {warns}）→ {args.out}")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 报告勾稽检查失败: {type(e).__name__}: {e}", file=sys.stderr)
        print("提示: 请确认 --link-map 文件存在且格式为 {\"links\": [...]}，"
              "并已先运行 parse_tax.py 生成解析结果。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
