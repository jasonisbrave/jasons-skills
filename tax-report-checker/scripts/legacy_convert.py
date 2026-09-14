#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧格式转换：.doc → .docx、.xls → .xlsx。

优先使用 Windows 上的 Microsoft Word/Excel COM（win32com.client）；
COM 不可用（未安装 Office / import 失败 / 调用异常）时回退 LibreOffice
（soffice --headless --convert-to ...）。两者都不可用 → 输出错误 JSON 并以退出码 2 退出。

用法:
    python legacy_convert.py <input.doc|input.xls> -o <outdir>
输出（单行 JSON）:
    {"ok": true, "output": "转换后的文件路径"}
    {"ok": false, "error": "..."}
原文件只读，不做任何修改。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 控制台输出防乱码（Python 3.7+）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Word/Excel SaveAs FileFormat 常量
DOCX_FILE_FORMAT = 16  # wdFormatXMLDocument
XLSX_FILE_FORMAT = 51  # xlOpenXMLWorkbook

SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]

UNAVAILABLE_MSG = "需要安装 Microsoft Office 或 LibreOffice 转换旧格式"


# ---------------------------------------------------------------- COM 引擎

def convert_with_com(src: Path, dst: Path, kind: str) -> Path:
    """用 Word/Excel COM 转换。成功返回 dst；失败抛异常。"""
    import win32com.client  # noqa: 延迟 import，未装 pywin32 时走回退

    app = None
    if kind == "doc":
        app = win32com.client.Dispatch("Word.Application")
        app.Visible = False
        try:
            app.DisplayAlerts = False
        except Exception:
            pass
        try:
            doc = app.Documents.Open(str(src), ReadOnly=True, AddToRecentFiles=False)
            try:
                doc.SaveAs2(str(dst), FileFormat=DOCX_FILE_FORMAT)
            except AttributeError:
                doc.SaveAs(str(dst), FileFormat=DOCX_FILE_FORMAT)
            finally:
                doc.Close(SaveChanges=False)
        finally:
            app.Quit()
    else:  # xls
        app = win32com.client.Dispatch("Excel.Application")
        app.Visible = False
        try:
            app.DisplayAlerts = False
        except Exception:
            pass
        try:
            # UpdateLinks=0：不更新外部链接，避免弹窗/误改数据
            wb = app.Workbooks.Open(str(src), ReadOnly=True, UpdateLinks=0)
            try:
                wb.SaveAs(str(dst), FileFormat=XLSX_FILE_FORMAT)
            finally:
                wb.Close(SaveChanges=False)
        finally:
            app.Quit()

    if not dst.exists():
        raise RuntimeError(f"COM 报告成功但未找到输出文件: {dst}")
    return dst


# ---------------------------------------------------------------- LibreOffice 回退

def find_soffice() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    for cand in SOFFICE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def convert_with_soffice(src: Path, outdir: Path, target: str) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise FileNotFoundError("未找到 soffice（PATH 与 Program Files 均无 LibreOffice）")
    cmd = [
        soffice, "--headless", "--norestore", "--convert-to", target,
        "--outdir", str(outdir), str(src),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    expected = outdir / (src.stem + "." + target)
    if proc.returncode == 0 and expected.exists():
        return expected
    detail = (proc.stderr or proc.stdout or "未知错误").strip()
    raise RuntimeError(f"LibreOffice 转换失败: {detail}")


# ---------------------------------------------------------------- 入口

def run(input_path: str | Path, outdir: str | Path) -> dict:
    """转换入口。返回 {"ok": bool, "output"/"error": str, ("engine": str, "unavailable": bool)}。"""
    src = Path(input_path)
    if not src.is_file():
        return {"ok": False, "error": f"文件不存在: {src}"}

    ext = src.suffix.lower()
    if ext == ".doc":
        kind, target = "doc", "docx"
    elif ext == ".xls":
        kind, target = "xls", "xlsx"
    else:
        return {"ok": False, "error": f"仅支持 .doc/.xls 旧格式，收到: {ext}（{src.name}）"}

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    dst = out / (src.stem + "." + target)
    if dst.exists():
        try:
            dst.unlink()
        except OSError:
            pass

    errors: list[str] = []
    unavailable = False

    # 1) Word/Excel COM
    try:
        convert_with_com(src, dst, kind)
        return {"ok": True, "output": str(dst), "engine": "com"}
    except ImportError:
        errors.append("COM: 未安装 pywin32")
        unavailable = True
    except Exception as e:
        msg = str(e).strip() or type(e).__name__
        errors.append(f"COM: {msg}")
        if any(k in msg for k in ("尚未调用 CoInitialize", "类未注册", "无效的类字符串",
                                  "0x80040154", "0x80070005")):
            unavailable = True

    # 2) LibreOffice
    try:
        result = convert_with_soffice(src, out, target)
        return {"ok": True, "output": str(result), "engine": "libreoffice"}
    except FileNotFoundError:
        errors.append("LibreOffice: 未找到 soffice")
        unavailable = True
    except Exception as e:
        errors.append(str(e).strip() or type(e).__name__)

    res = {"ok": False, "error": "；".join(errors)}
    if unavailable:
        res["error"] = UNAVAILABLE_MSG + "（" + "；".join(errors) + "）"
        res["unavailable"] = True
    return res


def main() -> None:
    parser = argparse.ArgumentParser(
        description="旧格式转换：.doc→.docx、.xls→.xlsx（COM 优先，LibreOffice 回退）",
    )
    parser.add_argument("input", help="输入文件（.doc 或 .xls）")
    parser.add_argument("-o", "--outdir", required=True, help="输出目录")
    args = parser.parse_args()

    try:
        result = run(args.input, args.outdir)
    except Exception as e:  # 不裸抛 traceback
        print(json.dumps({"ok": False, "error": f"转换异常: {type(e).__name__}: {e}"},
                         ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        sys.exit(2 if result.get("unavailable") else 1)


if __name__ == "__main__":
    main()
