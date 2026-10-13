# -*- coding: utf-8 -*-
"""分页的核心逻辑：类型判断、印刷页码、三种找章节的办法、导出的目录结构。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymupdf
import pytest

import books
import split_book as sb
from conftest import FakeLLM


def inspect(path):
    doc = pymupdf.open(path)
    pages = [sb.inspect_page(page) for page in doc]
    doc.close()
    texts, numbers, kinds, natives = map(list, zip(*pages))
    return texts, numbers, kinds, natives


# ------------------------------------------------------------ 名称

def test_clean_title():
    assert sb.clean_title("示例图书 (Z-Library)") == "示例图书"
    assert sb.clean_title("计算机网络(原书第8版) ( etc.) (z-library.sk, 1lib.sk, z-lib.sk)") == "计算机网络(原书第8版)"
    assert sb.clean_title('a/b:c?"d"') == "a b c d"
    assert sb.clean_title("...") == "未命名"


def test_title_key():
    assert sb.title_key("第1章 绪论") == "绪论"
    assert sb.title_key("第一部分 基础") == "基础"
    assert sb.title_key("Chapter 3 Transport Layer") == "transportlayer"
    assert sb.title_key("附录A 习题答案") == "习题答案"
    assert sb.title_key("参考文献") == "参考文献"
    assert sb.title_key("第三章") == "第三章"  # 只有编号时用编号本身


# ------------------------------------------------------------ 文字版还是扫描版

def test_text_book_is_text(library):
    texts, _, kinds, natives = inspect(library.text)
    assert sb.book_kind(texts, kinds) == ("text", True)
    assert set(natives) == {0}


def test_scanned_book_is_scan(library):
    texts, _, kinds, natives = inspect(library.scan)
    assert sb.book_kind(texts, kinds) == ("scan", False)
    assert set(kinds) == {"scan"} and set(natives) == {books.HEIGHT * 2}


def test_scan_with_ocr_layer(library):
    texts, _, kinds, _ = inspect(library.ocr)
    assert sb.book_kind(texts, kinds) == ("scan+ocr", True)


def test_watermark_line_is_not_a_text_layer(library):
    texts, _, kinds, _ = inspect(library.watermark)
    assert all(texts)  # 每页都能提取到那行水印
    assert sb.book_kind(texts, kinds) == ("scan", False)


def test_text_over_background_image_is_text(library):
    texts, _, kinds, natives = inspect(library.background)
    assert sb.book_kind(texts, kinds) == ("text", True)
    assert set(natives) == {0}  # 不能按那张低分辨率底图来限制导出尺寸


def test_sniff(library):
    assert sb.sniff(library.text) == {"kind": "文字版", "pages": books.PAGES}
    assert sb.sniff(library.scan) == {"kind": "扫描版", "pages": books.PAGES}
    assert sb.sniff(library.ocr) == {"kind": "扫描版（带 OCR 文字层）", "pages": books.PAGES}


# ------------------------------------------------------------ 印刷页码

def test_printed_page_numbers(library):
    _, numbers, _, _ = inspect(library.text)
    printed = sb.resolve_printed({i: found for i, found in enumerate(numbers) if found}, books.PAGES)
    assert printed[:books.OFFSET] == [None] * books.OFFSET
    # 章首页不印页码，也要靠前后页补出来
    assert printed[books.OFFSET:] == list(range(1, books.PAGES - books.OFFSET + 1))


def test_stray_numbers_are_ignored():
    cands = {i: {i - 9} for i in range(10, 60)}  # 真页码：PDF 页 = 印刷页 + 10
    cands[20].add(2024)
    cands[3] = {88}
    printed = sb.resolve_printed(cands, 60)
    assert printed[20] == 11 and printed[3] is None


def test_sampled_page_numbers_tolerate_one_misread():
    cands = {i: {i - 9} for i in range(12, 100, 8)}
    cands[52] = {7}  # 抽查时读错了一页
    printed = sb.resolve_printed(cands, 100, sampled=True)
    assert printed[52] == 43 and printed[99] == 90 and printed[5] is None
    assert sb.printed_lookup(printed)(1) == 11


# ------------------------------------------------------------ 章节：书签

def test_chapters_from_bookmarks(library):
    doc = pymupdf.open(library.text)
    chapters = sb.chapters_from_bookmarks(doc.get_toc(), doc.page_count)
    assert [(c.title, c.start) for c in chapters] == [(t, p) for level, t, p in books.BOOKMARKS if level == 1]


def test_bookmarks_under_a_single_root():
    toc = [[1, "书名", 1], [2, "第一章", 3], [2, "第二章", 10], [3, "2.1", 11]]
    assert [c.title for c in sb.chapters_from_bookmarks(toc, 20)] == ["书名", "第一章", "第二章"]


def test_useless_bookmarks_are_rejected():
    assert sb.chapters_from_bookmarks([[1, "封面", 1], [1, "正文", 2]], 20) == []
    assert sb.chapters_from_bookmarks([[1, f"第 {p} 页", p] for p in range(1, 21)], 20) == []


# ------------------------------------------------------------ 整本书走一遍

def test_split_by_bookmarks(library, split):
    info, first, out, _ = split(library.text)
    assert out.name == "示例图书"
    assert info["kind"] == "文字版" and info["source"] == "PDF 书签" and info["pages"] == books.PAGES
    assert [s["folder"] for s in info["sections"]] == [
        "00_封面", "01_前言", "02_目录", "03_第1章 绪论", "04_第2章 线性表", "05_第3章 栈和队列", "06_参考文献"]
    assert first["第1章 绪论"] == "0005_p1.jpg" and first["第2章 线性表"] == "0013_p9.jpg" and first["封面"] == "0001.jpg"
    assert len(list(out.rglob("*.jpg"))) == books.PAGES
    chapter_text = (out / "03_第1章 绪论" / sb.TEXT_FILE).read_text(encoding="utf-8")
    assert "===== 0005_p1 =====" in chapter_text and "第1章 绪论的第 1 行正文" in chapter_text
    index = (out / sb.MARK_DONE).read_text(encoding="utf-8")
    assert "  - 1.1 什么是数据结构　0006–0008" in index  # 小节常在页中结束，范围含下一小节开始的那一页
    assert "  - 1.2 算法分析　0008–0012" in index  # 后面是新的一章，就不含了
    assert sb.book_state(out) == "done"


def test_already_split_books_are_skipped_unless_forced(library, split):
    assert split(library.text)[0]["source"] == "PDF 书签"
    assert split(library.text)[0] == "skipped"
    assert split(library.text, force=True)[0]["source"] == "PDF 书签"


def test_interrupted_run_is_redone(library, split):
    _, _, out, _ = split(library.text)
    (out / sb.MARK_BUSY).touch()
    assert sb.book_state(out) == "partial"
    assert split(library.text)[0]["source"] == "PDF 书签"
    assert sb.book_state(out) == "done"


def test_foreign_folder_is_left_alone(library, split, tmp_path):
    mine = tmp_path / "out" / "示例图书"
    mine.mkdir(parents=True)
    (mine / "我的笔记.txt").write_text("别删", encoding="utf-8")
    status, _, _, logs = split(library.text, force=True)
    assert status == "skipped" and (mine / "我的笔记.txt").exists() and "不是本工具生成的" in logs[-1]


def test_no_bookmarks_and_no_model(library, split):
    info, _, out, logs = split(library.plain)
    assert [s["folder"] for s in info["sections"]] == ["全书"] and info["source"].startswith("无")
    assert len(list((out / "全书").glob("*.jpg"))) == books.PAGES
    assert any("没有填 API Key" in line for line in logs)


def test_export_options(library, split):
    _, _, out, _ = split(library.text, format="png", long_edge=800, no_text=True)
    image = pymupdf.Pixmap(str(next((out / "03_第1章 绪论").glob("*.png"))))
    assert max(image.width, image.height) == 800
    assert not list(out.rglob("*.txt"))


# ------------------------------------------------------------ 文字版，没有书签

def test_text_flow(library, split):
    llm = FakeLLM()
    info, first, _, _ = split(library.plain, llm)
    assert info["kind"] == "文字版" and info["source"] == "大模型读目录文字，本地核对"
    assert llm.calls == ["toc-text"] and llm.images == 0  # 只发了一次文字，没有发图片
    assert [s["folder"] for s in info["sections"]] == [
        "00_前置页", "01_第1章 绪论", "02_第2章 线性表", "03_第3章 栈和队列", "04_参考文献"]
    for title, start in books.STARTS.items():
        assert first[title].startswith(f"{start:04d}_p{start - books.OFFSET}")
    assert all(s["checked"] for s in info["sections"])


def test_text_flow_corrects_a_misread_page_number(library, split):
    llm = FakeLLM(misread={"第2章 线性表": 12, "第3章 栈和队列": 14})  # 实际是 9 和 16
    info, first, _, _ = split(library.plain, llm)
    assert first["第2章 线性表"].startswith("0013") and first["第3章 栈和队列"].startswith("0020")
    assert all(s["checked"] for s in info["sections"])


def test_text_flow_marks_what_it_cannot_verify(library, split):
    chapters = {("第2章 查无此章" if title.startswith("第2章") else title): start for title, start in books.STARTS.items()}
    info, first, _, _ = split(library.plain, FakeLLM(chapters=chapters))
    assert first["第2章 查无此章"].startswith("0013")  # 按目录上的页码推算
    assert [s["folder"] for s in info["sections"] if not s["checked"]] == ["02_第2章 查无此章"]


def test_ignoring_bookmarks_on_request(library, split):
    info, _, _, _ = split(library.text, FakeLLM(), no_bookmarks=True)
    assert info["source"] == "大模型读目录文字，本地核对"


# ------------------------------------------------------------ 扫描版，没有书签

def test_scan_flow(library, split):
    llm = FakeLLM()
    info, first, out, _ = split(library.scan, llm)
    assert info["kind"] == "扫描版" and info["source"] == "大模型看图识别目录"
    assert llm.calls.count("toc-image") == 1 and llm.calls.count("probe") == 1 and "toc-text" not in llm.calls
    for title, start in books.STARTS.items():
        assert first[title] == f"{start:04d}_p{start - books.OFFSET}.jpg"
    assert all(s["checked"] for s in info["sections"])
    assert not list(out.rglob("*.txt"))
    image = pymupdf.Pixmap(str(out / "01_第1章 绪论" / "0005_p1.jpg"))
    assert max(image.width, image.height) == books.HEIGHT * 2  # 没有放大到 1600


def test_scan_flow_without_readable_page_numbers(library, split):
    info, first, _, _ = split(library.scan, FakeLLM(page_numbers=False))
    assert [first[title] for title in books.STARTS] == [f"{start:04d}.jpg" for start in books.STARTS.values()]
    assert all(s["checked"] for s in info["sections"])


def test_scan_flow_marks_what_it_cannot_verify(library, split):
    info, first, _, _ = split(library.scan, FakeLLM(blind=["第2章 线性表"]))
    assert first["第2章 线性表"] == "0013_p9.jpg"
    assert [s["folder"] for s in info["sections"] if not s["checked"]] == ["02_第2章 线性表"]


def test_scan_with_ocr_reads_page_numbers_locally(library, split):
    llm = FakeLLM()
    info, first, out, _ = split(library.ocr, llm)
    assert info["kind"] == "扫描版（带 OCR 文字层）"
    assert "probe" not in llm.calls and llm.calls.count("toc-image") == 1
    assert first["第1章 绪论"] == "0005_p1.jpg"
    assert (out / "01_第1章 绪论" / sb.TEXT_FILE).exists()


def test_model_failure_still_exports_pages(library, split):
    class Broken:
        tokens_in = tokens_out = 0

        def ask(self, *args, **kwargs):
            raise sb.LLMError("接口返回 401", 401)

    info, _, out, logs = split(library.scan, Broken())
    assert [s["folder"] for s in info["sections"]] == ["全书"]
    assert any("调用大模型失败" in line for line in logs) and len(list(out.rglob("*.jpg"))) == books.PAGES


# ------------------------------------------------------------ 调接口的那一层

@pytest.fixture
def model_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.seen.append((self.path, body))
            if self.headers.get("Authorization") != "Bearer good-key":
                status, reply = 401, {"error": {"message": "invalid api key"}}
            else:
                status, reply = 200, {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}],
                                      "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
            raw = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def test_llm_client(model_server):
    config = {"api_key": "good-key", "base_url": f"http://127.0.0.1:{model_server.server_address[1]}/v1/",
              "model": "some-model", "extra_body": {"thinking": {"type": "disabled"}}}
    llm = sb.LLM(config, timeout=10)
    assert llm.ask("系统提示", "问题", 100) == {"ok": True}  # 包在 ```json 里的回答也能读
    assert (llm.tokens_in, llm.tokens_out) == (12, 3)
    path, body = model_server.seen[0]
    assert path == "/v1/chat/completions" and body["model"] == "some-model"
    assert body["response_format"] == {"type": "json_object"} and body["thinking"] == {"type": "disabled"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]

    with pytest.raises(sb.LLMError) as failure:
        sb.LLM({**config, "api_key": "bad-key"}, timeout=10).ask("系统提示", "问题", 100)
    assert failure.value.code == 401 and len(model_server.seen) == 2  # 密钥不对不重试
