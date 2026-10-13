# -*- coding: utf-8 -*-
"""网页界面背后的本地服务：接口、设置、安全限制。"""
import http.client
import json
import shutil
import threading
import time
from urllib.parse import quote

import pytest

import app
import books

HEADERS = {"Content-Type": "application/json", "X-Requested-With": app.APP_NAME}


@pytest.fixture
def site(library, tmp_path):
    """起一个只装了两本书的分页器：带书签的文字版、扫描版。"""
    shelf = tmp_path / "书库"
    shelf.mkdir()
    shutil.copy(library.text, shelf / library.text.name)
    shutil.copy(library.scan, shelf / library.scan.name)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"folder": str(shelf)}), encoding="utf-8")
    splitter = app.Splitter(config)
    app.Handler.app = splitter
    server = app.Server(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(method, path, body=None, headers=HEADERS, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, body=data, headers={**headers, **({"Host": host} if host else {})})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw

    def wait(condition, seconds=90):
        deadline = time.time() + seconds
        while time.time() < deadline:
            state = call("GET", "/api/state")[1]
            if condition(state):
                return state
            time.sleep(0.2)
        raise AssertionError("等了太久")

    call.wait, call.shelf, call.config, call.tmp = wait, shelf, config, tmp_path
    yield call
    server.shutdown()
    server.server_close()
    if splitter.pool:
        splitter.pool.shutdown()


def test_page_and_state(site):
    status, page = site("GET", "/")
    assert status == 200 and "电子书分页器".encode() in page
    state = site.wait(lambda s: not s["sniffing"])
    assert [(b["title"], b["kind"], b["pages"], b["state"]) for b in state["books"]] == [
        ("示例图书", "文字版", books.PAGES, "new"), ("示例图书·扫描版", "扫描版", books.PAGES, "new")]
    assert state["config"]["output"] == "" and state["config"]["has_key"] is False


def test_only_local_pages_may_call(site):
    assert site("GET", "/api/state", host="evil.example.com")[0] == 403
    status, data = site("POST", "/api/config", {"long_edge": 1200}, headers={"Content-Type": "application/json"})
    assert status == 400 and "来源" in data["error"]
    assert site("POST", "/api/start", {"names": ["../../别处的书.pdf"]})[0] == 400
    assert site("POST", "/api/config", b"[1, 2]")[0] == 400


def test_key_is_saved_but_never_sent_back(site):
    assert site("POST", "/api/config", {"api_key": "  sk-test-0000-1234  "})[0] == 200
    assert json.loads(site.config.read_text(encoding="utf-8"))["api_key"] == "sk-test-0000-1234"
    status, state = site("GET", "/api/state")
    assert state["config"]["has_key"] is True and state["config"]["key_hint"] == "1234"
    assert "sk-test-0000-1234" not in json.dumps(state)
    assert "sk-test-0000-1234".encode() not in site("GET", "/")[1]


def test_settings_are_validated(site):
    site("POST", "/api/config", {"api_key": "sk-test-0000-1234"})
    assert site("POST", "/api/config", {"long_edge": 99999, "quality": 1, "format": "webp", "export_text": False})[0] == 200
    config = site("GET", "/api/state")[1]["config"]
    assert (config["long_edge"], config["quality"], config["format"], config["export_text"]) == (4000, 30, "webp", False)
    assert site("POST", "/api/config", {"format": "bmp"})[0] == 400
    assert site("POST", "/api/config", {"base_url": "ftp://example.com"})[0] == 400
    assert json.loads(site.config.read_text(encoding="utf-8"))["api_key"] == "sk-test-0000-1234"  # 改别的设置不动 key


def test_split_a_book_through_the_api(site):
    name = "示例图书 (Z-Library).pdf"
    assert site("POST", "/api/start", {"names": [name], "force": False})[1] == {"added": 1}
    state = site.wait(lambda s: not s["busy"])
    book = state["books"][0]
    assert book["state"] == "done" and book["job"]["status"] == "done" and book["kind"] == "文字版"
    assert book["info"] == {"source": "PDF 书签", "folders": 7, "unchecked": 0}
    assert any("从书签里读到 7 个章节" in line for line in book["job"]["log"])
    assert (site.shelf / "示例图书" / "03_第1章 绪论" / "0005_p1.jpg").exists()
    status, detail = site("GET", "/api/book?name=" + quote(name))
    assert [s["folder"] for s in detail["sections"]][3:5] == ["03_第1章 绪论", "04_第2章 线性表"]


def test_choose_where_to_export(site):
    name = "示例图书 (Z-Library).pdf"
    target = site.tmp / "导出" / "图片"
    assert site("POST", "/api/folder", {"which": "output", "path": "相对路径"})[0] == 400
    assert site("POST", "/api/folder", {"which": "别的", "path": str(target)})[0] == 400
    assert site("POST", "/api/folder", {"which": "output", "path": str(target)})[0] == 200
    assert target.is_dir() and site("GET", "/api/state")[1]["config"]["output"] == str(target)
    site("POST", "/api/start", {"names": [name]})
    site.wait(lambda s: not s["busy"])
    assert (target / "示例图书" / "目录.md").exists() and not (site.shelf / "示例图书").exists()
    assert site("POST", "/api/folder", {"which": "output", "path": ""})[0] == 200
    state = site("GET", "/api/state")[1]
    assert state["config"]["output"] == "" and state["books"][0]["state"] == "new"  # 默认位置下这本书还没分过


def test_switch_library_folder(site):
    other = site.tmp / "另一个书库"
    other.mkdir()
    assert site("POST", "/api/folder", {"path": str(site.tmp / "不存在")})[0] == 400
    assert site("POST", "/api/folder", {"path": f'"{other}"'})[0] == 200  # 从资源管理器复制的路径带引号
    assert site("GET", "/api/state")[1]["books"] == []


def test_upload(site, library):
    upload = {"X-Requested-With": app.APP_NAME}
    assert site("POST", "/api/upload?name=" + quote("笔记.txt"), b"hello", headers=upload)[0] == 400
    data = library.ocr.read_bytes()
    assert site("POST", "/api/upload?name=" + quote("新书 (z-lib.org).pdf"), data, headers=upload)[0] == 200
    status, answer = site("POST", "/api/upload?name=" + quote("新书 (z-lib.org).pdf"), data, headers=upload)
    assert status == 400 and "已经有" in answer["error"]
    assert not list(site.shelf.glob("*.part"))
    state = site.wait(lambda s: not s["sniffing"])
    added = [b for b in state["books"] if b["name"].startswith("新书")][0]
    assert (added["title"], added["kind"]) == ("新书", "扫描版（带 OCR 文字层）")


def test_cancel_queue_and_busy_guard(site):
    names = [b["name"] for b in site("GET", "/api/state")[1]["books"]]
    assert site("POST", "/api/start", {"names": names})[1] == {"added": 2}
    assert site("POST", "/api/folder", {"path": str(site.tmp)})[0] == 400  # 有书在处理时不能换书库
    site("POST", "/api/cancel", {})
    statuses = [(b.get("job") or {}).get("status") for b in site("GET", "/api/state")[1]["books"]]
    assert "queued" not in statuses
    site.wait(lambda s: not s["busy"])
