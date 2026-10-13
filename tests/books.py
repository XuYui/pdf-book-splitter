# -*- coding: utf-8 -*-
"""测试用的书：用 PyMuPDF 现造，不依赖任何真实的电子书。

一本 30 页的《示例图书》：封面、两页前言、一页目录，然后是三章正文和参考文献。
正文页的页眉印着页码（章首页不印），PDF 页 = 印刷页码 + 4。
"""
import pymupdf

FONT = "china-s"  # PyMuPDF 自带的中文字体
WIDTH, HEIGHT = 420, 595
OFFSET = 4
CHAPTERS = [("第1章 绪论", 8), ("第2章 线性表", 7), ("第3章 栈和队列", 9), ("参考文献", 2)]
PAGES = OFFSET + sum(length for _, length in CHAPTERS)

# 章标题 -> 起始的 PDF 页
STARTS = {}
_page = OFFSET + 1
for _title, _length in CHAPTERS:
    STARTS[_title] = _page
    _page += _length

BOOKMARKS = [[1, "封面", 1], [1, "前言", 2], [1, "目录", 4]]
for _title, _start in STARTS.items():
    BOOKMARKS.append([1, _title, _start])
    if _title.startswith("第1章"):
        BOOKMARKS.append([2, "1.1 什么是数据结构", _start + 1])
        BOOKMARKS.append([2, "1.2 算法分析", _start + 3])


def _body(page, top, label):
    for row in range(14):
        text = f"这是{label}的第 {row + 1} 行正文，用来检验文字层的提取和按章导出。"
        page.insert_text((40, top + row * 22), text, fontname=FONT, fontsize=10.5)


def text_book(path, bookmarks=True):
    """文字版。bookmarks=False 时不带书签。"""
    doc = pymupdf.open()
    for number in range(1, PAGES + 1):
        page = doc.new_page(width=WIDTH, height=HEIGHT)
        if number == 1:
            page.insert_text((130, 260), "示例图书", fontname=FONT, fontsize=30)
        elif number in (2, 3):
            if number == 2:
                page.insert_text((40, 110), "前言", fontname=FONT, fontsize=20)
            _body(page, 160, "前言")
        elif number == 4:
            page.insert_text((40, 110), "目录", fontname=FONT, fontsize=20)
            for row, (title, start) in enumerate(STARTS.items()):
                page.insert_text((40, 160 + row * 26), f"{title} ………………………… {start - OFFSET}",
                                 fontname=FONT, fontsize=10.5)
        else:
            title = [t for t, start in STARTS.items() if start <= number][-1]
            if number == STARTS[title]:  # 章首页：大标题，不印页码
                page.insert_text((40, 120), title, fontname=FONT, fontsize=22)
                _body(page, 180, title)
            else:
                page.insert_text((40, 34), str(number - OFFSET), fontname=FONT, fontsize=9)
                page.insert_text((300, 34), title, fontname=FONT, fontsize=9)
                _body(page, 90, title)
    if bookmarks:
        doc.set_toc(BOOKMARKS)
    doc.save(path)
    doc.close()
    return path


def _photo(page, zoom=2):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY)
    return pix.tobytes("jpeg", jpg_quality=70)


def scanned_book(source, path, ocr=False, watermark=False, pages=None):
    """把文字版的每一页拍成图，做成扫描版。原图长边是 595 × 2 = 1190 像素。

    ocr=True 时在图上叠一层看不见的文字（带 OCR 文字层的扫描版）；
    watermark=True 时在每页图上加一行看得见的水印文字。
    """
    src, doc = pymupdf.open(source), pymupdf.open()
    font = pymupdf.Font(FONT)
    for index in range(pages or src.page_count):
        page = doc.new_page(width=WIDTH, height=HEIGHT)
        page.insert_image(page.rect, stream=_photo(src[index]))
        if ocr:
            writer = pymupdf.TextWriter(page.rect)
            for x0, y0, x1, y1, word, *_ in src[index].get_text("words"):
                writer.append((x0, y1 - 2), word, font=font, fontsize=max(4, (y1 - y0) * 0.75))
            writer.write_text(page, render_mode=3)
        if watermark:
            page.insert_text((40, HEIGHT - 14), "Downloaded from example-library.org 仅供学习交流",
                             fontname=FONT, fontsize=9)
    doc.save(path)
    doc.close()
    src.close()
    return path


def text_on_background(source, path, pages=6):
    """文字版，但每页铺了一张低分辨率的整页底图。它不该被当成扫描版。"""
    src, doc = pymupdf.open(source), pymupdf.open()
    paper = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 105, 149), False)
    paper.clear_with(244)
    for index in range(OFFSET, OFFSET + pages):
        page = doc.new_page(width=WIDTH, height=HEIGHT)
        page.insert_image(page.rect, stream=paper.tobytes("jpeg"))
        page.show_pdf_page(page.rect, src, index)
    doc.save(path)
    doc.close()
    src.close()
    return path
