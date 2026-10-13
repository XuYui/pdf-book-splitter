# -*- coding: utf-8 -*-
"""PDF 电子书分页器的网页界面：在本机起一个小服务，用浏览器操作。

    python app.py        启动并打开浏览器（也可以双击「启动分页器.bat」）

服务只监听 127.0.0.1，别的电脑访问不到。API Key 保存在 config.json 里，不会回传给页面。
"""
import argparse
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pymupdf

import split_book as sb

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "book-splitter"
UI_DEFAULTS = {
    "folder": "",  # 书库：放 PDF 的文件夹，空着就是程序所在的文件夹
    "output": "",  # 导出位置，空着就是和 PDF 放在一起
    "long_edge": 1600,
    "format": "jpg",
    "quality": 80,
    "export_text": True,
    "ignore_bookmarks": False,
}
KEY_HINTS = {
    400: "接口不接受这个请求，多半是这个模型不支持图片输入。",
    401: "API Key 不对，接口拒绝了它。",
    402: "账户余额不足。",
    404: "接口地址或模型名称不对。",
}
PICK_FOLDER = """
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
sys.stdout.reconfigure(encoding="utf-8")
print(filedialog.askdirectory(initialdir=sys.argv[1], title=sys.argv[2], parent=root))
"""


class Problem(Exception):
    """要原样告诉用户的错误。"""


class Body:
    """请求体：记着还剩多少没读，出错时好把剩下的读掉，连接才能继续用。"""

    def __init__(self, stream, length):
        self.stream, self.left = stream, length

    def read(self, size):
        chunk = self.stream.read(min(size, self.left))
        self.left -= len(chunk)
        return chunk

    def drain(self):
        while self.left > 0 and self.read(1 << 20):
            pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # Windows 上开着它会和已占用端口的程序抢同一个端口
    daemon_threads = True


class Splitter:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.lock = threading.Lock()
        self.jobs = {}  # PDF 路径 -> 这本书最近一次任务的状态
        self.todo = queue.Queue()
        self.pool = None
        self.pool_lock = threading.Lock()
        self.index_cache = {}
        self.sniffed = {}  # (PDF 路径, 大小, 修改时间) -> 抽查出的类型和页数；None 表示还在查
        # PyMuPDF 不能多线程用：本进程里只有干活的那个线程碰它，别的都交给进程池
        blank = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
        blank.clear_with(255)
        self.blank_image = "data:image/jpeg;base64," + base64.b64encode(blank.tobytes("jpeg")).decode("ascii")
        threading.Thread(target=self.work, daemon=True).start()

    def workers(self):
        with self.pool_lock:
            if self.pool is None:
                self.pool = ProcessPoolExecutor(min(16, os.cpu_count() or 1))
            return self.pool

    # ------------------------------------------------------------ 配置

    def config(self):
        config = {**UI_DEFAULTS, **sb.load_config(self.config_path)}
        folder = Path(config["folder"]) if config["folder"] else APP_DIR
        config["folder"] = str(folder if folder.is_dir() else APP_DIR)
        return config

    def save(self, changes):
        with self.lock:
            stored = {}
            if self.config_path.exists():
                stored = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            stored.update(changes)
            temp = self.config_path.with_suffix(".tmp")
            temp.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(self.config_path)

    def update_config(self, data):
        changes = {}
        if "api_key" in data:
            changes["api_key"] = str(data["api_key"]).strip()
        for field in ("base_url", "model"):
            if field in data:
                value = str(data[field]).strip()
                if not value or (field == "base_url" and not value.startswith(("http://", "https://"))):
                    raise Problem("接口地址要以 http:// 或 https:// 开头，模型名称不能为空。")
                changes[field] = value
        for field, low, high in (("long_edge", 600, 4000), ("quality", 30, 95)):
            if field in data:
                changes[field] = min(max(int(data[field]), low), high)
        if "format" in data:
            if data["format"] not in ("jpg", "png", "webp"):
                raise Problem("图片格式只能是 jpg、png 或 webp。")
            changes["format"] = data["format"]
        for field in ("export_text", "ignore_bookmarks"):
            if field in data:
                changes[field] = bool(data[field])
        self.save(changes)

    def out_dir(self, pdf, config):
        return sb.book_dir(pdf, config["output"] or None)

    def set_folder(self, which, path):
        """which: folder 书库 / output 导出位置。导出位置传空字符串表示恢复成和 PDF 放在一起。"""
        if which not in ("folder", "output"):
            raise Problem("不认识的文件夹类型。")
        if self.busy():
            raise Problem("还有书在处理，等它们做完再换文件夹。")
        text = str(path).strip().strip('"')
        if which == "output" and not text:
            return self.save({"output": ""})
        folder = Path(text)
        if not folder.is_absolute():
            raise Problem("请填完整的路径，比如 D:\\电子书。")
        if which == "output":
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as err:
                raise Problem(f"建不了这个文件夹：{err}")
        if not folder.is_dir():
            raise Problem("找不到这个文件夹。")
        self.save({which: str(folder.resolve())})

    def pick_folder(self, which):
        if which not in ("folder", "output"):
            raise Problem("不认识的文件夹类型。")
        config = self.config()
        title = "选择导出到哪个文件夹" if which == "output" else "选择放电子书的文件夹"
        result = subprocess.run([sys.executable, "-c", PICK_FOLDER, config.get(which) or config["folder"], title],
                                capture_output=True, encoding="utf-8", timeout=900)
        chosen = result.stdout.strip()
        if chosen:
            self.set_folder(which, chosen)
        return bool(chosen)

    def test_key(self):
        config = self.config()
        if not config["api_key"]:
            return False, "还没有填写 API Key。"
        llm = sb.LLM(config, timeout=40)
        system, question = "你是连通性测试助手，只输出 json。", '这是一次连通性测试，请只输出 json：{"ok": true}'
        with_image = [{"type": "text", "text": question},
                      {"type": "image_url", "image_url": {"url": self.blank_image}}]
        try:
            llm.ask(system, with_image, 50, tries=1)
            return True, f"连接正常，{config['model']} 接受图片输入，文字版和扫描版都能处理。"
        except sb.LLMError as err:
            if err.code != 400:
                return False, f"{KEY_HINTS.get(err.code, '连接失败。')}（{err}）"
        try:  # 带图片的请求被拒绝了，看看只发文字行不行
            llm.ask(system, question, 50, tries=1)
        except sb.LLMError as err:
            return False, f"{KEY_HINTS.get(err.code, '连接失败。')}（{err}）"
        return True, f"连接正常，但 {config['model']} 不接受图片：文字版的书能处理，扫描版要换一个支持图片的模型。"

    # ------------------------------------------------------------ 书库

    def busy(self):
        with self.lock:
            return any(job["status"] in ("queued", "running") for job in self.jobs.values())

    def index_info(self, out):
        """分好页的书的概要；目录.md 没变就用缓存，免得每次轮询都重读。"""
        path = out / sb.MARK_DONE
        try:
            stamp = path.stat().st_mtime_ns
            cached = self.index_cache.get(str(path))
            if not cached or cached[0] != stamp:
                cached = self.index_cache[str(path)] = (stamp, sb.read_index(path))
            return cached[1]
        except (OSError, ValueError):
            return {"pages": 0, "kind": "", "source": "", "sections": []}

    def sniff(self, pdf, stat):
        """还没分页的书先抽查几页看是文字版还是扫描版；在进程池里查，查完之前返回 None。"""
        key = (str(pdf), stat.st_size, stat.st_mtime_ns)
        if key not in self.sniffed:
            self.sniffed[key] = None

            def done(future, key=key):
                self.sniffed[key] = {"kind": "", "pages": 0} if future.exception() else future.result()

            try:
                self.workers().submit(sb.sniff, str(pdf)).add_done_callback(done)
            except RuntimeError:  # 进程池坏了，这本先不显示类型
                self.sniffed[key] = {"kind": "", "pages": 0}
        return self.sniffed[key]

    def state(self):
        config = self.config()
        folder = Path(config["folder"])
        books = []
        for pdf in sorted(folder.glob("*.pdf"), key=lambda p: p.name.lower()):
            out = self.out_dir(pdf, config)
            try:
                stat = pdf.stat()
                book = {"name": pdf.name, "title": sb.clean_title(pdf.stem), "size": stat.st_size,
                        "state": sb.book_state(out)}
            except OSError:
                continue  # 刚好被删掉或移走了
            if book["state"] == "done":
                info = self.index_info(out)
                book.update(kind=info["kind"], pages=info["pages"])
                book["info"] = {"source": info["source"], "folders": len(info["sections"]),
                                "unchecked": sum(not s["checked"] for s in info["sections"])}
            if not book.get("kind"):  # 还没分页，或者是旧版本分的、目录里没记类型
                book.update(self.sniff(pdf, stat) or {"kind": None, "pages": book.get("pages", 0)})
            with self.lock:
                job = self.jobs.get(str(pdf))
                if job:
                    book["job"] = {**job, "log": list(job["log"])}
            books.append(book)
        key = config["api_key"]
        shown = {field: config[field] for field in ("base_url", "model", *UI_DEFAULTS)}
        return {"config": {**shown, "has_key": bool(key), "key_hint": key[-4:] if len(key) >= 12 else ""},
                "busy": self.busy(), "sniffing": any(book["kind"] is None for book in books), "books": books}

    def book(self, name):
        return self.index_info(self.out_dir(self.pdf_path(name), self.config()))

    def pdf_path(self, name):
        pdf = Path(self.config()["folder"]) / Path(str(name)).name
        if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
            raise Problem("书库里没有这本书。")
        return pdf

    def upload(self, name, body):
        if Path(name).suffix.lower() != ".pdf":
            raise Problem("只能添加 PDF 文件。")
        target = Path(self.config()["folder"]) / (sb.safe_name(Path(name).stem, 120) + ".pdf")
        if target.exists():
            raise Problem(f"书库里已经有「{target.name}」了。")
        temp = target.with_name(target.name + ".part")
        try:
            with open(temp, "wb") as file:
                while chunk := body.read(1 << 20):
                    file.write(chunk)
            if body.left:
                raise Problem("上传中断了。")
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)

    def open_folder(self, name, which):
        """打开某本书导出的文件夹；没给书名时打开书库（folder）或导出位置（output）。"""
        config = self.config()
        folder = Path(config["output"] or config["folder"]) if which == "output" else Path(config["folder"])
        if name:
            out = self.out_dir(self.pdf_path(name), config)
            folder = out if out.is_dir() else folder
        if not folder.is_dir():
            raise Problem("这个文件夹还不存在。")
        if sys.platform == "win32":
            os.startfile(str(folder))
        else:  # 只在 Windows 上实测过；别的系统交给各自的「打开」命令
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])

    # ------------------------------------------------------------ 任务

    def start(self, names, force):
        added = 0
        for name in names:
            pdf = self.pdf_path(name)
            with self.lock:
                job = self.jobs.get(str(pdf))
                if job and job["status"] in ("queued", "running"):
                    continue
                self.jobs[str(pdf)] = {"status": "queued", "message": "排队中", "log": [],
                                       "done": 0, "total": 0, "force": bool(force)}
            self.todo.put(pdf)
            added += 1
        return added

    def cancel(self):
        with self.lock:
            for key in [key for key, job in self.jobs.items() if job["status"] == "queued"]:
                del self.jobs[key]

    def work(self):
        while True:
            pdf = self.todo.get()
            with self.lock:
                job = self.jobs.get(str(pdf))
                if not job or job["status"] != "queued":
                    continue  # 排队时被取消了
                job["status"], job["message"] = "running", "正在读取 PDF"

            def log(message, job=job):
                with self.lock:
                    job["log"].append(message)
                    job["message"] = message

            def progress(done, total, job=job):
                with self.lock:
                    job["done"], job["total"], job["message"] = done, total, "正在导出图片"

            try:
                config = self.config()
                llm = sb.LLM(config) if config["api_key"] else None
                args = SimpleNamespace(
                    out=config["output"] or None, long_edge=config["long_edge"], format=config["format"], quality=config["quality"],
                    no_text=not config["export_text"], no_bookmarks=config["ignore_bookmarks"],
                    force=job["force"], toc_scan_pages=int(config["toc_scan_pages"]),
                )
                status = sb.process(pdf, args, llm, self.workers(), log, progress)
                if llm and llm.tokens_in:
                    log(f"大模型用量：输入 {llm.tokens_in} tokens，输出 {llm.tokens_out} tokens")
            except Exception as err:  # 一本书出错不影响后面的书
                if isinstance(err, BrokenProcessPool):
                    with self.pool_lock:
                        self.pool = None
                log(f"处理失败：{err}")
                status = "error"
            with self.lock:
                job["status"] = status


class Handler(BaseHTTPRequestHandler):
    app = None  # 启动时填入 Splitter
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def local_only(self):
        """只认从本机地址发来的请求，免得别的网站借浏览器来操作这个服务。"""
        port = self.server.server_address[1]
        if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return True
        self.reply(403, {"error": "只能从本机访问。"})
        return False

    def do_GET(self):
        if not self.local_only():
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/":
                self.reply(200, (APP_DIR / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/ping":
                self.reply(200, {"app": APP_NAME})
            elif url.path == "/api/state":
                self.reply(200, self.app.state())
            elif url.path == "/api/book":
                self.reply(200, self.app.book(query.get("name", [""])[0]))
            else:
                self.reply(404, {"error": "没有这个地址。"})
        except Problem as err:
            self.reply(400, {"error": str(err)})

    def do_POST(self):
        body = Body(self.rfile, int(self.headers.get("Content-Length") or 0))
        try:
            self.route_post(body)
        except Problem as err:
            body.drain()
            self.reply(400, {"error": str(err)})
        except Exception as err:  # 界面上要能看到出了什么错
            body.drain()
            self.reply(500, {"error": f"出错了：{err}"})

    def route_post(self, body):
        if not self.local_only():
            return
        if self.headers.get("X-Requested-With") != APP_NAME:
            raise Problem("请求来源不对。")
        url = urlparse(self.path)
        app = self.app
        if url.path == "/api/upload":
            app.upload(parse_qs(url.query).get("name", [""])[0], body)
            return self.reply(200, {"ok": True})
        data = json.loads(body.read(body.left) or b"{}")
        if not isinstance(data, dict):
            raise Problem("请求格式不对。")
        if url.path == "/api/config":
            app.update_config(data)
            self.reply(200, {"ok": True})
        elif url.path == "/api/test-key":
            ok, message = app.test_key()
            self.reply(200, {"ok": ok, "message": message})
        elif url.path == "/api/folder":
            app.set_folder(data.get("which", "folder"), data.get("path", ""))
            self.reply(200, {"ok": True})
        elif url.path == "/api/pick-folder":
            self.reply(200, {"ok": app.pick_folder(data.get("which", "folder"))})
        elif url.path == "/api/start":
            self.reply(200, {"added": app.start(data.get("names") or [], data.get("force"))})
        elif url.path == "/api/cancel":
            app.cancel()
            self.reply(200, {"ok": True})
        elif url.path == "/api/open":
            app.open_folder(data.get("name"), data.get("which", "folder"))
            self.reply(200, {"ok": True})
        else:
            self.reply(404, {"error": "没有这个地址。"})


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as response:
            return json.load(response).get("app") == APP_NAME
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description="PDF 电子书分页器（网页界面）")
    parser.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    parser.add_argument("--config", default=str(APP_DIR / "config.json"), help="配置文件的位置")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    sys.stdout.reconfigure(errors="replace")
    pymupdf.TOOLS.mupdf_display_errors(False)
    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            if already_running(port):
                print(f"分页器已经在运行了：http://127.0.0.1:{port}")
                if not args.no_browser:
                    webbrowser.open(f"http://127.0.0.1:{port}")
                return
            # 端口被别的程序占了，换下一个
    if server is None:
        sys.exit(f"端口 {args.port}–{args.port + 9} 都被占用了，可以用 --port 指定别的端口。")
    Handler.app = Splitter(args.config)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"分页器已启动：{url}")
    print("用完以后，关掉这个窗口就退出了。")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
