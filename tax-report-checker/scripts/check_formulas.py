#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公式健康检查（check_type="公式健康"）。

用法:
    python check_formulas.py <outdir>/sheets.json -o results.json

规则:
1. 公式错误值       缓存值为 #REF!/#DIV/0!/#VALUE!/#N/A/#NAME?/#NULL!/#NUM! → error
2. 外部链接引用     公式含 [n] 引用（如 =[1]汇总表!A4）→ warning
3. 跨表引用断链     引用的 sheet 在本 workbook 中不存在 → error
4. 公式区硬编码     同一行相邻数值区中公式≥4 个且占比≥70%，中间夹数字常量 → warning
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

from _common import (group_rows, load_json, make_result, dump_json,  # noqa: E402
                     setup_console, split_coord)

CHECK_TYPE = "公式健康"

# ---- 阈值常量（可按需调整） ----
HARDCODE_MIN_FORMULAS = 4      # 一行内公式≥4 个才判断（误报宁可少）
HARDCODE_RATIO = 0.70          # 公式占比阈值
HARDCODE_MAX_PER_SHEET = 10    # 每张表硬编码告警上限（防止未填写模板刷屏）

ERROR_CELL_VALUES = {"#REF!", "#DIV/0!", "#VALUE!", "#N/A", "#NAME?", "#NULL!", "#NUM!"}

EXTERNAL_REF_RE = re.compile(r"\[\d+\]")
# 外部引用整段（[1]汇总表! / [1]'Sheet A'!），从断链扫描中剔除
EXTERNAL_TOKEN_RE = re.compile(r"\[\d+\](?:'(?:[^']|'')+'|[^!\s,()（）:：]+)!")
# 跨表引用的表名部分：'quoted name'! 或裸名!
# 裸名：不含空白与 ASCII 特殊符号（= & + - 等运算符不属于表名，未加引号的含 - 表名本就必须引号包裹）
SHEET_REF_RE = re.compile(r"(?:'((?:[^']|'')+)\'!|([^\s!\"#$%&'()*+,\-/:;<=>?@\[\\\]^`{|}~]+)!)")
# 公式内的字符串字面量（先剔除，避免 "xx!" 干扰表名解析）
STRING_LITERAL_RE = re.compile(r'"[^"]*"')


def _norm_sheet_name(raw: str) -> str:
    return raw.replace("''", "'").strip()


def check_formula_cell(formula: str, sheet_names: set) -> tuple[str | None, str | None, list[str]]:
    """返回 (外部链接?, 断链表名列表)。外部链接与断链互斥（外部引用不算断链）。"""
    if EXTERNAL_REF_RE.search(formula):
        return "external", None, []
    cleaned = STRING_LITERAL_RE.sub('""', EXTERNAL_TOKEN_RE.sub("", formula))
    broken: list[str] = []
    for m in SHEET_REF_RE.finditer(cleaned):
        quoted, bare = m.group(1), m.group(2)
        name = _norm_sheet_name(quoted) if quoted else (bare or "").strip()
        if not name or ":" in name:  # 三维引用 'S1:S2'! 不判断
            continue
        if name not in sheet_names and name not in broken:
            broken.append(name)
    return None, (None if not broken else "broken"), broken


def check_hardcode_row(items: list) -> tuple[int, int, list[str]] | None:
    """一行内连续数值单元格区段判断。返回 (公式数, 常量数, 常量coord列表) 或 None。"""
    best = None
    run: list = [items[0]]
    for prev, cur in zip(items, items[1:]):
        if cur[0] == prev[0] + 1:  # 列号相邻
            run.append(cur)
        else:
            if best is None:
                best = _judge_run(run)
            run = [cur]
    if best is None:
        best = _judge_run(run)
    return best


def _judge_run(run: list) -> tuple[int, int, list[str]] | None:
    n_f = sum(1 for _, _, e in run if "f" in e)
    n_c = len(run) - n_f
    if n_f < HARDCODE_MIN_FORMULAS or n_c < 1:
        return None
    if n_f / (n_f + n_c) < HARDCODE_RATIO:
        return None
    consts = [coord for _, coord, e in run if "f" not in e]
    return n_f, n_c, consts


def run(sheets_json: str | Path) -> list[dict]:
    """公式健康检查入口。返回 results 列表。"""
    data = load_json(sheets_json)
    results: list[dict] = []
    stat = {"错误值": 0, "外部链接": 0, "断链": 0, "硬编码": 0}

    for fname, fdata in data.items():
        sheets = fdata.get("sheets", {})
        for sheet_name, sdata in sheets.items():
            cells = sdata.get("cells", {})
            sheet_names = set(sheets.keys())
            hardcode_count = 0

            # 逐格：错误值 / 外部链接 / 断链
            for coord, entry in sorted(cells.items()):
                f = entry.get("f", "")
                v = entry.get("v")
                loc = f"{fname}#{sheet_name}!{coord}"

                # 1) 错误值（与是否公式格无关，凡缓存值是错误值都报）
                if isinstance(v, str) and v.strip() in ERROR_CELL_VALUES:
                    stat["错误值"] += 1
                    results.append(make_result(
                        CHECK_TYPE, "公式错误值", "error", False,
                        f"单元格缓存值为错误值 {v.strip()}",
                        expected="无错误值", actual=v.strip(),
                        source_location=loc, context=f[:120],
                    ))
                    continue

                if not f:
                    continue

                # 2) 外部链接引用
                kind, _, broken = check_formula_cell(f, sheet_names)
                if kind == "external":
                    stat["外部链接"] += 1
                    results.append(make_result(
                        CHECK_TYPE, "外部链接引用", "warning", False,
                        f"公式包含外部链接引用（依赖其他工作簿，数据可能不同步）",
                        source_location=loc, context=f[:120],
                    ))
                    continue

                # 3) 跨表引用断链
                if broken:
                    stat["断链"] += 1
                    results.append(make_result(
                        CHECK_TYPE, "跨表引用断链", "error", False,
                        f"公式引用的表在本工作簿中不存在: {'、'.join(broken)}",
                        expected=f"被引用的表存在（{'、'.join(broken)}）", actual="表不存在",
                        source_location=loc, context=f[:120],
                    ))

            # 4) 公式区硬编码（按行扫描）
            for row, items in sorted(group_rows(cells).items()):
                if hardcode_count >= HARDCODE_MAX_PER_SHEET:
                    break
                numeric = [it for it in items
                           if ("f" in it[2])
                           or (isinstance(it[2].get("v"), (int, float))
                               and not isinstance(it[2].get("v"), bool))]
                if len(numeric) < HARDCODE_MIN_FORMULAS + 1:
                    continue
                judged = check_hardcode_row(numeric)
                if not judged:
                    continue
                n_f, n_c, consts = judged
                hardcode_count += 1
                stat["硬编码"] += 1
                results.append(make_result(
                    CHECK_TYPE, "公式区硬编码", "warning", False,
                    f"第 {row} 行数值区 {n_f + n_c} 格中公式 {n_f} 个（{n_f / (n_f + n_c):.0%}），"
                    f"中间夹数字常量: {'、'.join(consts)}，疑似公式被硬编码覆盖",
                    expected="该行应全部为公式", actual=f"常量 {'、'.join(consts)}",
                    source_location=f"{fname}#{sheet_name}!第{row}行",
                    evidence=f"公式{n_f}/常量{n_c}，占比{n_f / (n_f + n_c):.0%}",
                ))

    stat_total = sum(stat.values())
    results.append(make_result(
        CHECK_TYPE, "公式健康统计", "info", True,
        f"共 {stat_total} 项发现：错误值 {stat['错误值']}、外部链接 {stat['外部链接']}、"
        f"断链 {stat['断链']}、硬编码 {stat['硬编码']}",
    ))
    return results


def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(description="公式健康检查（输入 sheets.json）")
    parser.add_argument("sheets_json", help="parse_tax.py 生成的 sheets.json 路径")
    parser.add_argument("-o", "--out", required=True, help="输出 results.json 路径")
    args = parser.parse_args()
    try:
        results = run(args.sheets_json)
        dump_json(args.out, results)
        errors = sum(1 for r in results if r["severity"] == "error")
        warns = sum(1 for r in results if r["severity"] == "warning")
        print(f"[check_formulas] 完成：{len(results)} 项结果（error {errors} / warning {warns}）→ {args.out}")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 公式健康检查失败: {type(e).__name__}: {e}", file=sys.stderr)
        print("提示: 请先运行 parse_tax.py 生成 sheets.json。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
