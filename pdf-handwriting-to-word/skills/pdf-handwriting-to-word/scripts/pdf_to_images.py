# -*- coding: utf-8 -*-
"""将 PDF 每页渲染为 PNG 图片，供后续逐页识别（手写扫描件 OCR/转写流程第一步）。

用法:
    python pdf_to_images.py <pdf路径> [输出目录] [dpi]

示例:
    .venv/Scripts/python pdf_to_images.py "习题.pdf" pdf_pages 200

输出:
    <输出目录>/page_01.png, page_02.png ...
依赖:
    pymupdf  ( pip install pymupdf )
"""
import sys
import os


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else "pdf_pages"
    dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 200

    import pymupdf  # 延迟导入，便于 --help 时无需依赖

    os.makedirs(outdir, exist_ok=True)
    doc = pymupdf.open(src)
    print(f"pages: {len(doc)}")
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=dpi)
        out = os.path.join(outdir, f"page_{i + 1:02d}.png")
        pix.save(out)
        print(out, pix.width, pix.height)


if __name__ == "__main__":
    main()
