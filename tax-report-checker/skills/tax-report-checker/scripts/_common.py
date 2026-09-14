#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tax-report-checker 共享工具。

被本目录下各脚本（parse_tax / check_formulas / check_values / check_completeness /
check_links / run_check / export_report）复用：

- setup_console: Windows 控制台 UTF-8 防乱码
- make_result:   统一 results 数组条目构造（schema 见 skill 说明）
- to_decimal / compare_with_tolerance / fmt_num: 全部数值比较走 Decimal，
  绝不用 float ==（字符串解析复用 calculator.py 的 parse_number）
- parse_report_text: 解析 report_text.md 的 SRC 注释结构
- group_rows / match_file_key / convert_cell_value 等通用小工具
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Optional

# 默认数值容差（元）
TOLERANCE = Decimal("0.01")

# results 条目的固定字段顺序
RESULT_KEYS = (
    "check_type", "rule_name", "severity", "passed", "description",
    "expected", "actual", "difference", "source_location", "context", "evidence",
)

_COORD_RE = re.compile(r"^([A-Za-z]{1,3})(\d+)$")


# ---------------------------------------------------------------- 控制台 / 路径

def setup_console() -> None:
    """Windows 控制台输出防乱码（Python 3.7+）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def script_dir() -> Path:
    """脚本所在目录（用于 import 同目录的 calculator.py / _common.py）。"""
    return Path(__file__).resolve().parent


def bootstrap_sys_path() -> None:
    """把脚本目录加入 sys.path，保证 `python 任意目录/xxx.py` 都能 import 同目录模块。"""
    here = script_dir()
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


# ---------------------------------------------------------------- results 条目

def make_result(
    check_type: str,
    rule_name: str,
    severity: str,
    passed: bool,
    description: str,
    expected: str = "",
    actual: str = "",
    difference: str = "",
    source_location: str = "",
    context: str = "",
    evidence: str = "",
    confidence: float | None = None,
) -> dict:
    """构造统一的 results 数组条目。severity 只用 error / warning / info。

    confidence 为可选字段，供 AI 检查项输出置信度；非 None 时写入结果。
    """
    result = {
        "check_type": check_type,
        "rule_name": rule_name,
        "severity": severity,
        "passed": bool(passed),
        "description": description,
        "expected": expected,
        "actual": actual,
        "difference": difference,
        "source_location": source_location,
        "context": context,
        "evidence": evidence,
    }
    if confidence is not None:
        result["confidence"] = float(confidence)
    return result


# ---------------------------------------------------------------- 数值（Decimal）

def to_decimal(value: Any) -> Decimal:
    """把 int/float/str/Decimal 转为 Decimal；失败抛 ValueError。

    字符串解析复用 calculator.py 的 parse_number（支持千分位、括号负数、横线、万元）。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("布尔值不是金额")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError("空字符串不是金额")
        bootstrap_sys_path()
        from calculator import parse_number  # 复用审计 skill 的算术工具
        return Decimal(str(parse_number(s)))
    raise ValueError(f"无法转换为数值: {value!r}")


def compare_with_tolerance(a: Decimal, b: Decimal, tol: Decimal = TOLERANCE) -> tuple[bool, Decimal]:
    """Decimal 相等比较（含容差）。返回 (是否相符, 绝对差)。绝不使用 float ==。"""
    diff = abs(a - b)
    return diff <= tol, diff


def fmt_num(value: Any) -> str:
    """展示用数字格式化：千分位 + 两位小数；非数值原样转字符串。"""
    try:
        d = value if isinstance(value, Decimal) else to_decimal(value)
    except Exception:
        return str(value)
    try:
        return f"{d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"
    except Exception:
        return str(d)


# ---------------------------------------------------------------- JSON 读写

def load_json(path: str | Path) -> Any:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: str | Path, obj: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return p


# ---------------------------------------------------------------- 单元格工具

def split_coord(coord: str) -> tuple[Optional[int], Optional[int]]:
    """'AB12' → (列号 28, 行号 12)；不合法返回 (None, None)。"""
    m = _COORD_RE.match(str(coord).strip())
    if not m:
        return None, None
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + (ord(ch) - ord("A") + 1)
    return col, int(m.group(2))


def group_rows(cells: dict) -> dict:
    """cells（sheets.json 里的 cells 字典）按行分组。

    返回 {行号: [(列号, coord, entry), ...]}，每行内按列号升序。
    """
    rows: dict[int, list] = {}
    for coord, entry in cells.items():
        col, row = split_coord(coord)
        if row is None:
            continue
        rows.setdefault(row, []).append((col, coord, entry))
    for items in rows.values():
        items.sort(key=lambda t: t[0])
    return rows


def iter_numeric_values(cells: dict) -> Iterable[tuple[str, Any]]:
    """产出 (coord, 数值)，跳过公式格、布尔、文本、None。"""
    for coord, entry in cells.items():
        if "f" in entry:
            continue
        v = entry.get("v")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        yield coord, v


def has_nonzero_numeric(cells: dict) -> bool:
    """金额区是否有任何非零数值缓存（模板未填时通常全为 0/空 → False）。"""
    for _, v in iter_numeric_values(cells):
        try:
            if to_decimal(v) != 0:
                return True
        except Exception:
            continue
    return False


def convert_cell_value(v: Any) -> Any:
    """openpyxl 单元格值 → JSON 可序列化值：数值 int/float、日期 ISO 字符串、其余 str。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() and abs(v) < 1e15 else v
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    return str(v)


# ---------------------------------------------------------------- report_text.md 解析

_SRC_RE = re.compile(
    r'<!--\s*SRC\s+file="(?P<file>[^"]+)"\s+(?P<kind>para|table)=(?P<idx>\d+)'
    r"(?:\s+row=(?P<row>\d+))?[^\n]*-->"
)


def parse_report_text(md_text: str) -> list[dict]:
    """把 report_text.md 解析成条目列表。

    每个条目: {"file": 文件名, "kind": "para"|"table", "idx": 序号, "row": 行号(table)或 None,
              "text": 该行原文}
    """
    entries: list[dict] = []
    cur: Optional[dict] = None
    row_counter = 0
    for line in md_text.splitlines():
        stripped = line.strip()
        m = _SRC_RE.match(stripped)
        if m:
            kind = m.group("kind")
            cur = {
                "file": m.group("file"),
                "kind": kind,
                "idx": int(m.group("idx")),
                "row": int(m.group("row")) if m.group("row") else None,
                "text": "",
            }
            row_counter = 0
            # para 与 table 一致：cur 保留，段落/表格内容由下一非空行填充
            continue
        if not stripped or stripped.startswith("<!--") or stripped.startswith("|---"):
            continue
        if cur is not None:
            row_counter += 1
            entries.append({
                "file": cur["file"], "kind": cur["kind"], "idx": cur["idx"],
                "row": cur["row"] if cur["kind"] == "table" and cur["row"] is not None else (row_counter if cur["kind"] == "table" else None),
                "text": stripped,
            })
            # 表格连续多行都归属同一 table 注释，cur 保留
            continue
    return entries


# ---------------------------------------------------------------- 文件名匹配

def match_file_key(keys: Iterable[str], wanted: str) -> Optional[str]:
    """在 keys（sheets.json / report_text.md 的文件名）中匹配 wanted。

    规则：精确 → 扩展名无关的 stem 匹配（.doc 转换为 .docx 后仍可命中）。
    找不到返回 None。
    """
    wanted = (wanted or "").strip().replace("\\", "/").split("/")[-1]
    if not wanted:
        return None
    keys = list(keys)
    for k in keys:
        if k == wanted:
            return k
    wstem = Path(wanted).stem.strip().lower()
    for k in keys:
        if Path(k).stem.strip().lower() == wstem:
            return k
    return None


def count_severities(results: list[dict]) -> dict:
    counts = {"error": 0, "warning": 0, "info": 0, "passed": 0}
    for r in results:
        if r.get("passed"):
            counts["passed"] += 1
        sev = r.get("severity")
        if sev in counts:
            counts[sev] += 1
    return counts
