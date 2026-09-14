#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""完整性检查（check_type="完整性"）。

用法:
    python check_completeness.py <outdir>/sheets.json [--text <outdir>/report_text.md] -o results.json

规则:
1. 填报勾选一致性  填报表单 C 列勾"√"但对应 sheet 金额区无非零数据 → warning "勾选未填报"；
                   反向（附表有数据但未勾选）→ info "已填报未勾选"
2. 财报差异列      利润表"校对"列（公式含 A100000 且相减）缓存值 |x|>0.01 → error
3. 必填项空白      基本信息!D6/D7/D8/D13/D15 任一为空 → error
4. 公司名一致性    各 xlsx"纳税人名称/单位名称"行右侧单元格 vs 报告前 50 段提取的公司名
"""

from __future__ import annotations

import argparse
import re
import sys
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

from _common import (TOLERANCE, dump_json, group_rows, has_nonzero_numeric,  # noqa: E402
                     load_json, make_result, parse_report_text, setup_console,
                     to_decimal)

CHECK_TYPE = "完整性"

FORM_SHEET_NAME = "企业所得税年度纳税申报表填报表单"
FORM_ID_RE = re.compile(r"^A\d{6}$")
BASIC_SHEET_NAME = "基本信息"
# 必填项：单元格 → 含义（模板"基本信息"sheet 布局）
REQUIRED_CELLS = {
    "D6": "单位名称",
    "D7": "纳税人识别号",
    "D8": "法定代表人",
    "D13": "所属年度",
    "D15": "会计准则",
}

LABEL_RE = re.compile(r"纳税人名称|单位名称")
COMPANY_HINT_RE = re.compile(r"[\u4e00-\u9fff（）()A-Za-z0-9]{2,30}(?:公司|事务所|协会)")
MAX_TEXT_PARAS = 50  # 公司名只取报告前 50 段
MAX_COMPANY_CANDIDATES = 20


# ---------------------------------------------------------------- 1. 填报勾选一致性

def check_form_selection(fname: str, fdata: dict, results: list[dict]) -> None:
    sheets = fdata.get("sheets", {})
    form = sheets.get(FORM_SHEET_NAME)
    if form is None:
        return
    rows = group_rows(form.get("cells", {}))
    for row, items in sorted(rows.items()):
        entry_a = next((e for _, coord, e in items if coord.startswith("A")), None)
        if entry_a is None:
            continue
        form_id = entry_a.get("v")
        if not (isinstance(form_id, str) and FORM_ID_RE.match(form_id.strip())):
            continue
        form_id = form_id.strip()
        entry_c = next((e for _, coord, e in items if coord.startswith("C")), None)
        cval = entry_c.get("v") if entry_c else None
        checked = isinstance(cval, str) and cval.strip() == "√"

        target_name = next((name for name in sheets if name.startswith(form_id)), None)
        if target_name is None:
            continue  # 表单编号没有对应 sheet，跳过
        filled = has_nonzero_numeric(sheets[target_name].get("cells", {}))
        loc = f"{fname}#{FORM_SHEET_NAME}!A{row}"

        if checked and not filled:
            results.append(make_result(
                CHECK_TYPE, "勾选未填报", "warning", False,
                f"表单 {form_id} 已勾选填报，但对应工作表“{target_name}”金额区无非零数据",
                expected="勾选的表单应已填报数据", actual="金额区无非零数值",
                source_location=loc,
            ))
        elif not checked and filled:
            results.append(make_result(
                CHECK_TYPE, "已填报未勾选", "info", False,
                f"工作表“{target_name}”已有非零数据，但填报表单中 {form_id} 未勾选（C 列={cval!r}）",
                expected="已填报的表单应勾选", actual="C 列未勾选",
                source_location=loc,
            ))


# ---------------------------------------------------------------- 2. 财报差异列

def check_profit_diff(fname: str, sheet_name: str, cells: dict, results: list[dict]) -> None:
    if "利润表" not in sheet_name:
        return
    for coord, entry in sorted(cells.items()):
        f = entry.get("f", "")
        if not (isinstance(f, str) and "A100000" in f and "-" in f):
            continue  # 差异校验模式：公式含主表且与另一单元格相减
        v = entry.get("v")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue  # 空/未计算/文本 → 不判
        diff = abs(to_decimal(v))
        if diff > TOLERANCE:
            results.append(make_result(
                CHECK_TYPE, "财报与申报表差异", "error", False,
                f"财报-申报表差异校验列数值不为零（差 {diff} 元）",
                expected="0.00", actual=str(v), difference=str(diff),
                source_location=f"{fname}#{sheet_name}!{coord}", context=f[:120],
            ))


# ---------------------------------------------------------------- 3. 必填项空白

def check_required(fname: str, fdata: dict, results: list[dict]) -> None:
    basic = fdata.get("sheets", {}).get(BASIC_SHEET_NAME)
    if basic is None:
        return
    cells = basic.get("cells", {})
    for coord, label in REQUIRED_CELLS.items():
        entry = cells.get(coord)
        v = entry.get("v") if entry else None
        if v is None or (isinstance(v, str) and not v.strip()):
            results.append(make_result(
                CHECK_TYPE, "基本信息必填项空白", "error", False,
                f"基本信息!{coord}（{label}）为空，属必填项",
                expected=label + " 已填写", actual="空",
                source_location=f"{fname}#{BASIC_SHEET_NAME}!{coord}",
            ))


# ---------------------------------------------------------------- 4. 公司名一致性

def extract_companies_from_sheets(data: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for fname, fdata in data.items():
        for sheet_name, sdata in fdata.get("sheets", {}).items():
            for row, items in group_rows(sdata.get("cells", {})).items():
                for col, coord, entry in items:
                    v = entry.get("v")
                    if not (isinstance(v, str) and LABEL_RE.search(v)):
                        continue
                    # 同一行右侧第一个非空文本单元格
                    for c2, coord2, e2 in items:
                        if c2 <= col:
                            continue
                        if c2 > col + 6:
                            break
                        v2 = e2.get("v")
                        if isinstance(v2, str) and v2.strip():
                            name = v2.strip().strip("：:　 ")
                            if COMPANY_HINT_RE.search(name):
                                out.append((name, f"{fname}#{sheet_name}!{coord2}"))
                            break
                if len(out) >= MAX_COMPANY_CANDIDATES:
                    return out
    return out


def extract_companies_from_text(entries: list[dict]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for e in entries:
        if e["kind"] != "para" or e["idx"] > MAX_TEXT_PARAS:
            continue
        found = COMPANY_HINT_RE.findall(e["text"])
        if found:
            best = max(found, key=len)
            out.append((best, f'{e["file"]}#para={e["idx"]}'))
    return out


def check_company_names(data: dict, report_text_path: Path | None, results: list[dict]) -> None:
    candidates: dict[str, list[str]] = {}
    for name, loc in extract_companies_from_sheets(data):
        candidates.setdefault(name, []).append(loc)
    if report_text_path is not None and report_text_path.exists():
        md = report_text_path.read_text(encoding="utf-8")
        for name, loc in extract_companies_from_text(parse_report_text(md)):
            candidates.setdefault(name, []).append(loc)

    if not candidates:
        return  # 无可提取的公司名 → 跳过
    if len(candidates) == 1:
        name, locs = next(iter(candidates.items()))
        results.append(make_result(
            CHECK_TYPE, "公司名一致", "info", True,
            f"各处公司名一致: {name}",
            actual=name, source_location=locs[0],
        ))
        return
    names = " / ".join(f"“{n}”({locs[0]})" for n, locs in list(candidates.items())[:6])
    first_locs = [ls[0] for ls in candidates.values()][:3]
    results.append(make_result(
        CHECK_TYPE, "公司名不一致", "warning", False,
        f"在不同文件中提取到 {len(candidates)} 个不同的公司名: {names}",
        expected="各文件公司名一致", actual=" / ".join(list(candidates.keys())[:6]),
        source_location="; ".join(first_locs),
    ))


# ---------------------------------------------------------------- 入口

def run(sheets_json: str | Path, report_text: str | Path | None = None) -> list[dict]:
    """完整性检查入口。返回 results 列表。"""
    data = load_json(sheets_json)
    results: list[dict] = []

    for fname, fdata in data.items():
        check_form_selection(fname, fdata, results)
        for sheet_name, sdata in fdata.get("sheets", {}).items():
            check_profit_diff(fname, sheet_name, sdata.get("cells", {}), results)
        check_required(fname, fdata, results)

    text_path = Path(report_text) if report_text else None
    check_company_names(data, text_path, results)
    return results


def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(description="完整性检查（勾选/财报差异/必填项/公司名）")
    parser.add_argument("sheets_json", help="parse_tax.py 生成的 sheets.json 路径")
    parser.add_argument("--text", default=None, help="report_text.md 路径（公司名提取用，可选）")
    parser.add_argument("-o", "--out", required=True, help="输出 results.json 路径")
    args = parser.parse_args()
    try:
        results = run(args.sheets_json, args.text)
        dump_json(args.out, results)
        errors = sum(1 for r in results if r["severity"] == "error")
        warns = sum(1 for r in results if r["severity"] == "warning")
        print(f"[check_completeness] 完成：{len(results)} 项结果（error {errors} / warning {warns}）→ {args.out}")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 完整性检查失败: {type(e).__name__}: {e}", file=sys.stderr)
        print("提示: 请先运行 parse_tax.py 生成 sheets.json。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
