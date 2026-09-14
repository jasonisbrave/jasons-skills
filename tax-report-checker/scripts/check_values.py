#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数值校验：对公式可安全求值的单元格，用缓存值重算并与该格缓存值比较（check_type="数值校验"）。

用法:
    python check_values.py <outdir>/sheets.json -o results.json

支持的公式模式（其余一律跳过）:
- =SUM(A1:A9)（含跨表、多参数、矩形区域）
- =A1+B2-C3（任意 +/- 混合、同表/跨表引用、裸数字、括号）
- =ROUND(<上述>, n)
- = 后直接跟单个引用
所有算术走 Decimal；容差 0.01。引用单元格缓存值为 null（公式未计算过）→ 该格跳过。
"""

from __future__ import annotations

import argparse
import re
import sys
from decimal import Decimal, ROUND_HALF_UP
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
                     load_json, make_result, setup_console, split_coord, to_decimal)

CHECK_TYPE = "数值校验"
ALLOWED_FUNCS = {"SUM", "ROUND"}
MAX_RANGE_CELLS = 20000  # 区域展开上限，防止整列引用


class Unsupported(ValueError):
    """公式超出安全求值支持范围 → 跳过该格（不是错误）。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


# ---------------------------------------------------------------- 词法

_SHEET_Q = r"'(?:[^']|'')*'"
_SHEET_BARE = r"[^\s!\"'$%&()*+\-/:;<=>@\[\]^`{|}~']+"
_REF_RE = re.compile(rf"(?:({_SHEET_Q}|{_SHEET_BARE})!)?(\$?[A-Za-z]{{1,3}}\$?\d+)(?::(\$?[A-Za-z]{{1,3}}\$?\d+))?")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_FUNC_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.\u4e00-\u9fff]*)\s*\(")


def _tokenize(s: str) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    pos = 0
    n = len(s)
    while pos < n:
        ch = s[pos]
        if ch == " " or ch == "\t":
            pos += 1
            continue
        if ch in "+-(),:":
            tokens.append((ch, ch))
            pos += 1
            continue
        if ch == ")":
            tokens.append((")", ")"))
            pos += 1
            continue
        m = _REF_RE.match(s, pos)
        if m:
            prefix = m.group(1)
            sheet = None
            if prefix:
                sheet = prefix[1:-1].replace("''", "'") if prefix.startswith("'") else prefix
            tokens.append(("REF", (sheet, m.group(2), m.group(3))))
            pos = m.end()
            continue
        m = _NUM_RE.match(s, pos)
        if m:
            tokens.append(("NUM", Decimal(m.group())))
            pos = m.end()
            continue
        m = _FUNC_RE.match(s, pos)
        if m:
            name = m.group(1).upper()
            if name not in ALLOWED_FUNCS:
                raise Unsupported("函数不支持", name)
            tokens.append((name, name))
            tokens.append(("(", "("))  # _FUNC_RE 把 "(" 一并消费了，压回一个左括号记号
            pos = m.end()
            continue
        raise Unsupported("语法不支持", f"字符 {ch!r}")
    return tokens


# ---------------------------------------------------------------- 求值器

class _Evaluator:
    def __init__(self, file_data: dict):
        self.sheets: dict = file_data.get("sheets", {})

    def _cells(self, sheet: str | None) -> dict:
        if sheet is None:
            # 无表名前缀 → 引用当前表由调用方注入；这里用占位（当前表名在 eval 时替换）
            raise Unsupported("内部错误", "缺少当前表名")
        sdata = self.sheets.get(sheet)
        if sdata is None:
            raise Unsupported("引用表不存在", sheet)
        return sdata.get("cells", {})

    def _ref_value(self, sheet: str | None, coord: str) -> Decimal:
        cells = self._cells(sheet)
        coord = coord.replace("$", "").upper()
        entry = cells.get(coord)
        if entry is None:
            return Decimal(0)  # 空单元格按 Excel 语义 = 0
        if "f" in entry and entry.get("v") is None:
            raise Unsupported("引用缓存缺失", coord)  # 引用格公式未计算过 → 整格跳过
        v = entry.get("v")
        if isinstance(v, bool) or v is None:
            if isinstance(v, bool):
                raise Unsupported("引用非数值", coord)
            return Decimal(0)
        if not isinstance(v, (int, float)):
            raise Unsupported("引用非数值", coord)
        return to_decimal(v)

    def _range_values(self, sheet: str | None, c1: str, c2: str) -> list[Decimal]:
        cells = self._cells(sheet)
        col1, row1 = split_coord(c1.replace("$", ""))
        col2, row2 = split_coord(c2.replace("$", ""))
        if col1 is None or row1 is None or col2 is None or row2 is None:
            raise Unsupported("语法不支持", f"区域 {c1}:{c2}")
        r1, r2 = min(row1, row2), max(row1, row2)
        k1, k2 = min(col1, col2), max(col1, col2)
        if (r2 - r1 + 1) * (k2 - k1 + 1) > MAX_RANGE_CELLS:
            raise Unsupported("范围过大", f"{c1}:{c2}")
        out: list[Decimal] = []
        for row in range(r1, r2 + 1):
            for col in range(k1, k2 + 1):
                entry = cells.get(f"{_col_letter(col)}{row}")
                if entry is None:
                    continue  # 空单元格 = 0
                if "f" in entry and entry.get("v") is None:
                    continue  # 区域内未计算格按 0（Excel 重算语义），不跳过整格
                v = entry.get("v")
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue  # SUM 忽略文本
                out.append(to_decimal(v))
        return out

    # ---- 递归下降: expr := factor (('+'|'-') factor)* ----
    def eval_tokens(self, tokens: list, cur_sheet: str) -> Decimal:
        self.cur_sheet = cur_sheet
        self.toks = tokens
        self.pos = 0
        val = self._expr()
        if self.pos != len(self.toks):
            raise Unsupported("语法不支持", "存在未消费的记号")
        return val

    def _peek(self) -> str | None:
        return self.toks[self.pos][0] if self.pos < len(self.toks) else None

    def _next(self) -> tuple[str, object]:
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def _expr(self) -> Decimal:
        val = self._factor()
        while self._peek() in ("+", "-"):
            op, _ = self._next()
            rhs = self._factor()
            val = val + rhs if op == "+" else val - rhs
        return val

    def _factor(self) -> Decimal:
        kind, val = self._next()
        if kind == "-":
            return -self._factor()
        if kind == "+":
            return self._factor()
        if kind == "(":
            inner = self._expr()
            if self._peek() != ")":
                raise Unsupported("语法不支持", "缺少右括号")
            self._next()
            return inner
        if kind == "NUM":
            return val  # type: ignore[return-value]
        if kind == "REF":
            sheet, c1, c2 = val  # type: ignore[misc]
            sheet = sheet if sheet is not None else self.cur_sheet
            if c2:  # 裸区域（不在 SUM 内）不支持
                raise Unsupported("语法不支持", f"区域 {c1}:{c2}")
            return self._ref_value(sheet, c1)
        if kind == "SUM":
            if self._peek() != "(":
                raise Unsupported("语法不支持", "SUM 缺左括号")
            self._next()
            total = Decimal(0)
            while True:
                tk, tv = self._next()
                if tk != "REF":
                    raise Unsupported("语法不支持", "SUM 参数须为引用")
                sheet, c1, c2 = tv  # type: ignore[misc]
                sheet = sheet if sheet is not None else self.cur_sheet
                if c2:
                    total += sum(self._range_values(sheet, c1, c2), Decimal(0))
                else:
                    total += self._ref_value(sheet, c1)
                if self._peek() == ",":
                    self._next()
                    continue
                break
            if self._peek() != ")":
                raise Unsupported("语法不支持", "SUM 缺右括号")
            self._next()
            return total
        if kind == "ROUND":
            if self._peek() != "(":
                raise Unsupported("语法不支持", "ROUND 缺左括号")
            self._next()
            inner = self._expr()
            if self._peek() != ",":
                raise Unsupported("语法不支持", "ROUND 缺位数参数")
            self._next()
            tk, tv = self._next()
            if tk != "NUM":
                raise Unsupported("语法不支持", "ROUND 位数须为数字")
            digits = int(tv)  # type: ignore[arg-type]
            if not 0 <= digits <= 15:
                raise Unsupported("语法不支持", f"ROUND 位数 {digits}")
            if self._peek() != ")":
                raise Unsupported("语法不支持", "ROUND 缺右括号")
            self._next()
            return inner.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
        raise Unsupported("语法不支持", f"记号 {kind}")


def _col_letter(col: int) -> str:
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


# ---------------------------------------------------------------- 主流程

def run(sheets_json: str | Path) -> list[dict]:
    """数值校验入口。返回 results 列表（含一条统计 info）。"""
    data = load_json(sheets_json)
    results: list[dict] = []
    evaluated = matched = mismatched = 0
    skip_reasons: dict[str, int] = {}

    for fname, fdata in data.items():
        evaluator = _Evaluator(fdata)
        for sheet_name, sdata in fdata.get("sheets", {}).items():
            cells = sdata.get("cells", {})
            for coord, entry in sorted(cells.items()):
                f = entry.get("f", "")
                if not (isinstance(f, str) and f.startswith("=")):
                    continue
                loc = f"{fname}#{sheet_name}!{coord}"
                v = entry.get("v")
                if "f" in entry and v is None:
                    skip_reasons["公式未计算(缓存为空)"] = skip_reasons.get("公式未计算(缓存为空)", 0) + 1
                    continue
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    skip_reasons["公式结果非数值"] = skip_reasons.get("公式结果非数值", 0) + 1
                    continue
                try:
                    tokens = _tokenize(f[1:])
                    expected = evaluator.eval_tokens(tokens, sheet_name)
                except Unsupported as e:
                    skip_reasons[e.reason] = skip_reasons.get(e.reason, 0) + 1
                    continue
                except Exception:
                    skip_reasons["语法不支持"] = skip_reasons.get("语法不支持", 0) + 1
                    continue

                evaluated += 1
                actual = to_decimal(v)
                ok, diff = compare_with_tolerance(expected, actual)
                if ok:
                    matched += 1
                else:
                    mismatched += 1
                    results.append(make_result(
                        CHECK_TYPE, "缓存值与公式重算不符", "warning", False,
                        f"公式重算结果与缓存值不一致（差 {fmt_num(diff)} 元）",
                        expected=fmt_num(expected), actual=fmt_num(actual),
                        difference=fmt_num(diff),
                        source_location=loc, context=f[:120],
                    ))

    skipped = sum(skip_reasons.values())
    reason_txt = "；".join(f"{k} {n}" for k, n in sorted(skip_reasons.items(), key=lambda x: -x[1]))
    results.append(make_result(
        CHECK_TYPE, "数值校验统计", "info", True,
        f"可求值 {evaluated}（相符 {matched}，不符 {mismatched}），跳过 {skipped}"
        + (f"（{reason_txt}）" if reason_txt else ""),
    ))
    return results


def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(description="数值校验（缓存值 vs 公式重算，Decimal）")
    parser.add_argument("sheets_json", help="parse_tax.py 生成的 sheets.json 路径")
    parser.add_argument("-o", "--out", required=True, help="输出 results.json 路径")
    args = parser.parse_args()
    try:
        results = run(args.sheets_json)
        dump_json(args.out, results)
        stat = next((r for r in results if r["rule_name"] == "数值校验统计"), None)
        print(f"[check_values] 完成：{len(results) - 1} 项发现 → {args.out}")
        if stat:
            print(f"  {stat['description']}")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 数值校验失败: {type(e).__name__}: {e}", file=sys.stderr)
        print("提示: 请先运行 parse_tax.py 生成 sheets.json。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
