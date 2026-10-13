# -*- coding: utf-8 -*-
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

import books  # noqa: E402
import split_book as sb  # noqa: E402


@pytest.fixture(scope="session")
def pool():
    with ProcessPoolExecutor(2) as executor:
        yield executor


@pytest.fixture(scope="session")
def library(tmp_path_factory):
    """各种版本的《示例图书》，整个测试过程只造一次。"""
    folder = tmp_path_factory.mktemp("books")
    text = books.text_book(folder / "示例图书 (Z-Library).pdf")
    return SimpleNamespace(
        folder=folder,
        text=text,
        plain=books.text_book(folder / "示例图书·无书签.pdf", bookmarks=False),
        scan=books.scanned_book(text, folder / "示例图书·扫描版.pdf"),
        ocr=books.scanned_book(text, folder / "示例图书·扫描带文字层.pdf", ocr=True),
        watermark=books.scanned_book(text, folder / "水印.pdf", watermark=True, pages=8),
        background=books.text_on_background(text, folder / "底图.pdf"),
    )


class FakeLLM:
    """假的大模型：照着 books.STARTS 作答，并记下每次被问了什么。

    misread: {章标题: 读错的印刷页码}；blind: 核对时故意认不出的章；
    page_numbers=False 表示抽查页码时一页也读不出来。
    """

    def __init__(self, misread=None, blind=(), page_numbers=True, chapters=None):
        self.misread, self.blind, self.page_numbers = misread or {}, set(blind), page_numbers
        self.chapters = chapters or books.STARTS
        self.calls, self.images, self.tokens_in, self.tokens_out = [], 0, 0, 0

    def ask(self, system, content, max_tokens, tries=3):
        if isinstance(content, str):
            pages, lead, images = [int(p) for p in re.findall(r"^PDF第(\d+)页：$", content, re.M)], content, 0
        else:
            texts = [part["text"] for part in content if part["type"] == "text"]
            pages = [int(m.group(1)) for t in texts for m in [re.fullmatch(r"PDF第(\d+)页：", t)] if m]
            lead = texts[0]
            images = sum(part["type"] == "image_url" for part in content)
            assert images == len(pages)
        self.images += images
        if system == sb.PROMPT_TOC:
            self.calls.append("toc-text" if isinstance(content, str) else "toc-image")
            return {"toc_pages": [4], "chapters": [
                {"title": title, "page": self.misread.get(title, start - books.OFFSET)}
                for title, start in self.chapters.items()]}
        if system == sb.PROMPT_PROBE:
            self.calls.append("probe")
            return {"pages": [{"pdf_page": p, "printed": p - books.OFFSET if self.page_numbers and p > books.OFFSET else None}
                              for p in pages]}
        if system == sb.PROMPT_START:
            self.calls.append("start")
            title = lead.split("：", 1)[1]
            start = books.STARTS.get(title)
            return {"start_page": start if title not in self.blind and start in pages else None}
        raise AssertionError("没见过的提示词")


@pytest.fixture
def split(pool, tmp_path):
    """给一本书分页，返回 (目录.md 里的概要, {章名: 第一张图的文件名}, 这本书的输出文件夹, 过程记录)。"""

    def run(pdf, llm=None, **options):
        args = SimpleNamespace(out=str(tmp_path / "out"), long_edge=1600, format="jpg", quality=80, no_text=False,
                               no_bookmarks=False, force=False, toc_scan_pages=40)
        vars(args).update(options)
        logs = []
        status = sb.process(Path(pdf), args, llm, pool, logs.append, lambda done, total: None)
        out = sb.book_dir(Path(pdf), args.out)
        if status != "done":
            return status, {}, out, logs
        info = sb.read_index(out / sb.MARK_DONE)
        first = {s["folder"].split("_", 1)[-1]: sorted(p.name for p in (out / s["folder"]).glob(f"*.{args.format}"))[0]
                 for s in info["sections"]}
        return info, first, out, logs

    return run
