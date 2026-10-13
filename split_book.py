# -*- coding: utf-8 -*-
"""PDF 电子书分页器：把 PDF 拆成逐页图片，并按章节放进不同的文件夹。

用法：
    python split_book.py                 处理本文件夹里的全部 PDF
    python split_book.py 书.pdf [...]    处理指定的 PDF（也可以给文件夹）
    python app.py                        网页界面

每本书先逐页检查，判断它是文字版还是扫描版，然后走各自的流程：

                文字版                          扫描版
    页码        从页眉页脚的文字里读            有 OCR 文字层就从里面读，否则请大模型抽查几页
    章节        书签；没有书签时把目录的文字    书签；没有书签时把前若干页的图片发给大模型，
                发给大模型，再在本地按字号      再把每章起始页前后几页的图片发给它核对
                找章标题核对
    输出        图片 + 每章的文字               图片（不超过原图分辨率）；有 OCR 文字层时也导出文字

调用大模型需要在 config.json 里填 api_key（或设置环境变量 DEEPSEEK_API_KEY）。
既没有书签也没有大模型时，所有页面放进同一个文件夹。

图片文件名形如 0057_p39.jpg：0057 是 PDF 里的第几页，p39 是书上印刷的页码（认不出时省略）。
"""
import argparse
import base64
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pymupdf

__version__ = "1.0.0"

APP_DIR = Path(__file__).resolve().parent
MARK_DONE = "目录.md"
MARK_BUSY = ".分页未完成"
TEXT_FILE = "本章文字.txt"
UNCHECKED = "（起始页未核对）"
KIND_LABELS = {"text": "文字版", "scan": "扫描版", "scan+ocr": "扫描版（带 OCR 文字层）"}

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "extra_body": {"thinking": {"type": "disabled"}},
    "toc_scan_pages": 40,
}


@dataclass
class Chapter:
    title: str
    start: int  # PDF 页码，从 1 开始
    checked: bool = True


@dataclass
class Section:
    folder: str
    title: str
    start: int
    end: int  # 含
    checked: bool = True


# ---------------------------------------------------------------- 名称处理

_JUNK = re.compile(
    r"[（(\[【][^（()）\[\]【】]*(?:z-?lib|1lib|libgen|anna|\betc\.)[^（()）\[\]【】]*[)）\]】]", re.I
)


def safe_name(text, limit=60):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .") or "未命名"


def clean_title(stem):
    """去掉文件名里下载站加的尾巴，例如 (z-library.sk, 1lib.sk, z-lib.sk)。"""
    return safe_name(_JUNK.sub(" ", stem).strip(" ._-") or stem, 80)


def to_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def squash(text):
    """去掉空白和标点，比较标题时用。"""
    return re.sub(r"[\W_]+", "", text).lower()


# ---------------------------------------------------------------- 打开与渲染

def open_pdf(path):
    doc = pymupdf.open(path)
    if doc.needs_pass and not doc.authenticate(""):
        doc.close()
        raise ValueError("PDF 有密码")
    return doc


def render_page(page, long_edge):
    zoom = long_edge / max(page.rect.width, page.rect.height)
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False)


def encode_image(pix, fmt, quality):
    if fmt == "jpg":
        return pix.tobytes("jpeg", jpg_quality=quality)
    if fmt == "png":
        return pix.tobytes("png")
    return pix.pil_tobytes(format="WEBP", quality=quality)  # 需要 Pillow


def _render_chunk(task):
    pdf_path, jobs, fmt, quality = task
    doc = open_pdf(pdf_path)
    for index, out_path, long_edge in jobs:
        Path(out_path).write_bytes(encode_image(render_page(doc[index], long_edge), fmt, quality))
    doc.close()
    return len(jobs)


def render_all(pool, pdf_path, jobs, fmt, quality, progress):
    """jobs: [(页序, 输出路径, 长边像素)]"""
    chunks = [jobs[i:i + 10] for i in range(0, len(jobs), 10)]
    done = 0
    for count in pool.map(_render_chunk, [(str(pdf_path), chunk, fmt, quality) for chunk in chunks]):
        done += count
        progress(done, len(jobs))


# ---------------------------------------------------------------- 逐页检查：文字版还是扫描版

_NUM = re.compile(r"\D{0,2}(\d{1,4})\D{0,2}")


def header_footer_numbers(words, height):
    """页眉、页脚区域里的整数，即页码的候选。"""
    digits = []
    for x0, y0, x1, y1, text, *_ in words:
        match = _NUM.fullmatch(text)
        if match and (y1 < height * 0.09 or y0 > height * 0.91):
            digits.append(((y0 + y1) / 2, x0, x1, y1 - y0, match.group(1)))
    digits.sort()
    lines = []
    for digit in digits:
        if lines and digit[0] - lines[-1][-1][0] < digit[3] * 0.5:
            lines[-1].append(digit)
        else:
            lines.append([digit])
    numbers = set()
    for line in lines:
        # 有些 PDF 把页码的每一位存成单独的词，顺序还是乱的，按横坐标拼回去
        line.sort(key=lambda d: d[1])
        runs = [[line[0]]]
        for digit in line[1:]:
            if digit[1] - runs[-1][-1][2] < digit[3] * 0.8:
                runs[-1].append(digit)
            else:
                runs.append([digit])
        for run in runs:
            joined = "".join(d[4] for d in run)
            if len(joined) <= 4 and int(joined) >= 1:
                numbers.add(int(joined))
    return numbers


def inspect_page(page):
    """检查一页，返回 (文字, 页眉页脚里的候选页码, 类别, 扫描原图的长边像素)。

    类别：text 文字页；scan 扫描页，即整页是一张（或拼起来的几张）图，上面没有看得见的文字，
    这时提取到的文字是 OCR 垫在图下面或设成不可见的那一层；other 空白页等。
    """
    textpage = page.get_textpage()
    text = page.get_text("text", textpage=textpage).strip()
    numbers = header_footer_numbers(page.get_text("words", textpage=textpage), page.rect.height)
    kind, native = "text" if len(text) >= 30 else "other", 0
    if not page.get_images():
        return text, numbers, kind, native
    area = page.rect.get_area()
    images = [(pymupdf.Rect(info["bbox"]), info["width"]) for info in page.get_image_info()]
    if sum(rect.get_area() for rect, _ in images) < 0.85 * area:
        return text, numbers, kind, native
    # 整页都是图。画在图上面的可见文字很少（没有，或只有一行水印）才算扫描页；
    # 文字版偶尔也会铺一张整页底图，那种页面图上面有大片可见文字。
    drawn = page.get_bboxlog()
    last_image = max((i for i, (what, bbox) in enumerate(drawn) if what in ("fill-image", "fill-imgmask")
                      and pymupdf.Rect(bbox).get_area() >= 0.02 * area), default=-1)
    visible_text = sum(pymupdf.Rect(bbox).get_area() for what, bbox in drawn[last_image + 1:]
                       if what in ("fill-text", "stroke-text"))
    if visible_text < 0.05 * area:
        # 原图每一点（1/72 英寸）有多少像素，据此算出原图相当于多长的边
        density = max((width / rect.width for rect, width in images if rect.get_area() >= 0.02 * area), default=0)
        kind, native = "scan", round(max(page.rect.width, page.rect.height) * density)
    return text, numbers, kind, native


def _inspect_chunk(task):
    pdf_path, indexes = task
    doc = open_pdf(pdf_path)
    result = [inspect_page(doc[i]) for i in indexes]
    doc.close()
    return result


def inspect_all(pool, pdf_path, n):
    """逐页检查整本书，返回四个等长的列表：文字、候选页码、类别、扫描原图的长边像素。"""
    chunks = [range(i, min(i + 20, n)) for i in range(0, n, 20)]
    pages = [item for chunk in pool.map(_inspect_chunk, [(str(pdf_path), c) for c in chunks]) for item in chunk]
    return tuple(map(list, zip(*pages))) if pages else ([], [], [], [])


def book_kind(texts, kinds):
    """整本书的类型 -> (类型, 有没有可用的文字层)。

    文字页比扫描页多就是文字版（text），否则按扫描版处理；扫描版里过半的页带文字，
    说明有 OCR 文字层（scan+ocr），没有就是纯图片（scan）。
    """
    # 按 100 个字算，免得把下载站在每页上加的一行水印当成文字层
    has_text = sum(len(text) >= 100 for text in texts) >= len(texts) / 2
    if kinds.count("text") > kinds.count("scan"):
        return "text", has_text
    return ("scan+ocr" if has_text else "scan"), has_text


def sniff(pdf_path):
    """抽查十来页，快速判断一本书的类型，给界面在分页之前显示用；正式分页时会逐页检查。"""
    doc = open_pdf(pdf_path)
    n = doc.page_count
    pages = [inspect_page(doc[i]) for i in sorted({n * k // 12 for k in range(12)})]
    doc.close()
    kind, _ = book_kind([page[0] for page in pages], [page[2] for page in pages])
    return {"kind": KIND_LABELS[kind], "pages": n}


# ---------------------------------------------------------------- 印刷页码

def resolve_printed(cands, n, sampled=False):
    """cands: {页序(从 0 起): {候选页码}} -> 长度为 n 的列表，元素是印刷页码或 None。

    真正的页码满足「PDF 页 - 印刷页码」在一大段里保持不变，靠这一点把页眉页脚里
    碰巧出现的其他数字筛掉。sampled 表示 cands 只是抽查了少数几页。
    """
    votes = Counter(i + 1 - v for i, numbers in cands.items() for v in numbers)
    need = 2 if sampled else max(4, n // 50)
    offset = {}
    for i, numbers in cands.items():
        hits = [i + 1 - v for v in numbers if votes[i + 1 - v] >= need]
        if hits:
            offset[i] = max(hits, key=votes.__getitem__)
    pages = sorted(offset)
    reach = n if sampled else 12
    # 两个偏移一致的页之间的页（比如不印页码的章首页）沿用同一个偏移
    for a, b in zip(pages, pages[1:]):
        if offset[a] == offset[b] and b - a <= reach:
            for i in range(a + 1, b):
                offset[i] = offset[a]
    if pages:
        first = pages[0]
        page_one = max(offset[first], 0)  # 印刷第 1 页所在的页序
        if first - page_one <= reach:
            for i in range(page_one, first):
                offset[i] = offset[first]
        if sampled:  # 抽查到的最后一页之后没有别的依据，沿用它的偏移
            for i in range(pages[-1] + 1, n):
                offset[i] = offset[pages[-1]]
    return [i + 1 - offset[i] if i in offset else None for i in range(n)]


def printed_lookup(printed):
    """返回「印刷页码 -> PDF 页码」的换算函数；一个页码都没认出来时返回 None。"""
    known = [(value, i + 1) for i, value in enumerate(printed) if value]
    if not known:
        return None
    exact = {}
    for value, page in known:
        exact.setdefault(value, page)

    def lookup(p):
        if p in exact:
            return exact[p]
        value, page = min(known, key=lambda item: abs(item[0] - p))
        return page + (p - value)

    return lookup


# ---------------------------------------------------------------- 章节：书签

def chapters_from_bookmarks(toc, n):
    """从书签里取「章」这一级的条目；书签不可用时返回 []。"""
    toc = [(level, title.strip(), page) for level, title, page in toc if 1 <= page <= n and title.strip()]
    if len(toc) < 3:
        return []
    # 取条目数够 3 个的最浅一级，避免只有一个「书名」根节点时分不出章
    base = next(lv for lv in sorted({e[0] for e in toc}) if sum(e[0] <= lv for e in toc) >= 3)
    chapters = []
    for level, title, page in toc:
        if level > base or (chapters and page < chapters[-1].start):
            continue
        if chapters and page == chapters[-1].start:
            chapters[-1].title = title
        else:
            chapters.append(Chapter(title, page))
    if len(chapters) < 2 or len(chapters) > n / 2:
        return []
    return chapters


# ---------------------------------------------------------------- 章节：大模型

PROMPT_TOC = """你是图书结构分析助手。用户会给出一本书最前面若干页的内容（页面图片，或者从页面里提取出的文字），每一页前面有一行「PDF第N页：」标明它是 PDF 的第几页。请找到其中的目录页，提取全书的章节，并输出 json。

要求：
1. 以「章」为单位提取。如果目录分「篇/部分」和「章」两级，只提取章；如果没有「章」的字样，就提取目录里最高一级的条目。不要提取小节（如 1.1、1.1.1）。
2. 目录里排在正文之后的附录、参考文献、索引等，也按顺序作为条目输出。
3. page 填目录上印刷的页码，必须是阿拉伯数字整数。页码是罗马数字或没有页码的条目（如序、前言）不要输出。
4. title 照抄目录原文，保留「第1章」这样的编号；按目录中的顺序输出。
5. toc_pages 填目录所在的 PDF 页。如果这些页里没有目录，chapters 和 toc_pages 都输出空数组。

json 输出示例：
{"toc_pages": [9, 10, 11], "chapters": [{"title": "第1章 绪论", "page": 1}, {"title": "第2章 线性表", "page": 23}, {"title": "参考文献", "page": 310}]}"""

PROMPT_START = """用户会给出一本书里连续的几页图片，每张图片前有一行文字标明它是 PDF 的第几页，并指定一个章节标题。请判断哪一页是这一章的起始页，即章标题作为大标题出现、这一章的正文从这里开始的那一页。页眉里重复出现的章名不算，目录页里出现的章名也不算。

输出 json：{"start_page": 71}。如果这几页里没有这一章的起始页，输出 {"start_page": null}。"""

PROMPT_PROBE = """用户会给出同一本书里的若干页图片，每张图片前有一行文字标明它是 PDF 的第几页。请读出每一页页眉或页脚处印刷的页码。只要阿拉伯数字的页码；没有页码、页码是罗马数字或看不清时填 null。

json 输出示例：
{"pages": [{"pdf_page": 40, "printed": 22}, {"pdf_page": 80, "printed": null}]}"""


class LLMError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code  # HTTP 状态码；网络不通或回答不合法时为 None


class LLM:
    """OpenAI 兼容的 chat/completions 接口。文字版只发文字，扫描版要发图片，需要模型支持图片输入。"""

    def __init__(self, config, timeout=300):
        self.url = config["base_url"].rstrip("/") + "/chat/completions"
        self.key = config["api_key"]
        self.model = config["model"]
        self.extra = config.get("extra_body") or {}
        self.timeout = timeout
        self.tokens_in = self.tokens_out = 0
        self._lock = threading.Lock()

    def ask(self, system, content, max_tokens, tries=3):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            **self.extra,
        }
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.key}
        problem, code = "", None
        for attempt in range(tries):
            if attempt:
                time.sleep(3 * attempt)
            try:
                request = urllib.request.Request(self.url, data=data, headers=headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    reply = json.load(response)
            except urllib.error.HTTPError as err:
                detail = err.read().decode("utf-8", "replace")[:300]
                problem, code = f"接口返回 {err.code}：{detail}", err.code
                if err.code in (429, 500, 502, 503, 504):
                    continue
                break
            except (OSError, ValueError) as err:
                problem, code = f"网络错误：{err}", None
                continue
            usage = reply.get("usage") or {}
            with self._lock:
                self.tokens_in += usage.get("prompt_tokens") or 0
                self.tokens_out += usage.get("completion_tokens") or 0
            text = (reply["choices"][0]["message"].get("content") or "").strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
            try:
                answer = json.loads(text)
            except ValueError:
                answer = None
            if isinstance(answer, dict):
                return answer
            problem, code = "模型没有返回合法的 JSON", None
        raise LLMError(problem, code)


def toc_entries(answer, k):
    """整理大模型读出的目录 -> ([(章标题, 印刷页码)], 正文最早可能开始的 PDF 页)。"""
    entries = []
    for item in answer.get("chapters") or []:
        if not isinstance(item, dict):
            continue
        title, page = str(item.get("title") or "").strip(), to_int(item.get("page"))
        if title and page and (not entries or page > entries[-1][1]):
            entries.append((title, page))
    toc_pages = [p for p in map(to_int, answer.get("toc_pages") or []) if p and p <= k]
    return entries, max(toc_pages, default=0) + 1


def settle(entries, guesses, found, body_start, n, log):
    """guesses 是按印刷页码推算的起始页，found 是核对出来的（没核对上为 None），定出各章的起始页。"""
    # 核对过的章先定下来，没核对上的只有在夹在前后两章之间时才采用推算值
    starts = {}
    for i in range(len(entries)):
        if found[i] and all(found[i] > s for s in starts.values()):
            starts[i] = found[i]
    for i in range(len(entries)):
        if i not in starts:
            before = max((s for j, s in starts.items() if j < i), default=body_start - 1)
            after = min((s for j, s in starts.items() if j > i), default=n + 1)
            if before < guesses[i] < after:
                starts[i] = guesses[i]
            else:
                log(f"定位不到「{entries[i][0]}」，它的页面会并入上一章。")
    return [Chapter(entries[i][0], starts[i], checked=starts[i] == found[i]) for i in sorted(starts)]


# ------------------------------------------------ 文字版：目录文字交给大模型，在本地核对

_NUMBERING = re.compile(
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百零〇两]+\s*(?:部分|[章篇部卷讲回节编])"
    r"|(?:chapter|part|appendix)\s+\w+|附录\s*[A-Za-z0-9]?|[0-9]+(?:\.[0-9]+)*)[\s.、:：]*", re.I)


def title_key(title):
    """章标题里用来在页面上找它的那部分：去掉「第3章」这类编号；只有编号时就用编号本身。"""
    name = squash(_NUMBERING.sub("", title, count=1))
    return name if len(name) >= 2 else squash(title)


def font_lines(page):
    """页面上的每一行文字 -> [(字的大小, 文字)]

    大小按字实际占的宽度算（汉字取字宽，西文取字宽的两倍），不用 PDF 里记的字号：
    有些排版软件导出的 PDF，正文记的字号是实际大小的两倍，标题却不是。
    页眉页脚不算在内，那里会重复出现章名。
    """
    lines = []
    height = page.rect.height
    for block in page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        for line in block.get("lines", []):
            if line["bbox"][3] < height * 0.09 or line["bbox"][1] > height * 0.91:
                continue
            chars = [c for span in line["spans"] for c in span["chars"] if c["c"].strip()]
            wide = [c["bbox"][2] - c["bbox"][0] for c in chars if "一" <= c["c"] <= "鿿"]
            narrow = [2 * (c["bbox"][2] - c["bbox"][0]) for c in chars if c["c"].isascii() and c["c"].isalnum()]
            sizes = sorted(wide or narrow)
            if sizes:
                lines.append((sizes[len(sizes) // 2], "".join(c["c"] for c in chars)))
    return lines


def body_font_size(doc):
    """正文的字有多大：抽查若干页，取字数最多的那个大小。"""
    weight = Counter()
    n = doc.page_count
    for i in sorted({n * k // 24 for k in range(24)}):
        for size, text in font_lines(doc[i]):
            weight[round(size * 2) / 2] += len(text)
    return weight.most_common(1)[0][0] if weight else 0


def chapters_from_text(doc, llm, texts, printed, scan_pages, log):
    """文字版没有书签时：大模型从目录文字里读出各章，再在本地找章标题核对。返回章节列表。"""
    n = doc.page_count
    k = min(n, scan_pages)
    log(f"没有可用的书签，把前 {k} 页的文字发给大模型识别目录…")
    pages = "\n\n".join(f"PDF第{p}页：\n{texts[p - 1][:3000]}" for p in range(1, k + 1))
    answer = llm.ask(PROMPT_TOC, "这是一本书前若干页提取出的文字。\n\n" + pages, 8000)
    entries, body_start = toc_entries(answer, k)
    if len(entries) < 2:
        log("大模型没有在这些页里找到目录。")
        return []
    log(f"目录里有 {len(entries)} 章，正在核对每一章的起始页…")

    # 章标题在章首页上是大字；页眉里重复的章名、正文里提到的章名都是小字，按字号就能分开
    body_size = body_font_size(doc)
    headings = {}

    def has_heading(p, key):
        if p not in headings:
            headings[p] = squash("".join(text for size, text in font_lines(doc[p - 1]) if size >= body_size * 1.2))
        return key in headings[p]

    lookup = printed_lookup(printed)
    if lookup is None:  # 这本书不印页码：从目录后面往后找第一章，用它算出偏移
        key = title_key(entries[0][0])
        first = next((p for p in range(body_start, n + 1) if has_heading(p, key)), None)
        if first is None:
            log("没能找到第一章从哪一页开始。")
            return []
        shift = first - entries[0][1]
        lookup = lambda p: p + shift
    guesses = [lookup(page) for _, page in entries]
    found = {}
    for i, (title, _) in enumerate(entries):
        key = title_key(title)
        near = range(max(body_start, guesses[i] - 7), min(n, guesses[i] + 7) + 1)
        found[i] = next((p for p in sorted(near, key=lambda p: (abs(p - guesses[i]), p)) if has_heading(p, key)), None)
    return settle(entries, guesses, found, body_start, n, log)


# ------------------------------------------------ 扫描版：页面图片交给大模型识别和核对

def pages_content(doc, pages, lead, natives):
    content = [{"type": "text", "text": lead}]
    for p in pages:
        jpeg = render_page(doc[p - 1], min(1400, natives[p - 1] or 1400)).tobytes("jpeg", jpg_quality=70)
        url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        content.append({"type": "text", "text": f"PDF第{p}页："})
        content.append({"type": "image_url", "image_url": {"url": url}})
    return content


def probe_printed(doc, llm, natives):
    """没有文字层：抽查十来页让大模型读页码，推算全书的印刷页码。"""
    n = doc.page_count
    pages = sorted({min(n, max(1, round(n * (k + 0.5) / 12))) for k in range(12)})
    answer = llm.ask(PROMPT_PROBE, pages_content(doc, pages, "请读出这些页的印刷页码。", natives), 1000)
    cands = {}
    for item in answer.get("pages") or []:
        if isinstance(item, dict):
            page, value = to_int(item.get("pdf_page")), to_int(item.get("printed"))
            if page in pages and value:
                cands[page - 1] = {value}
    return resolve_printed(cands, n, sampled=True)


def find_first_chapter(doc, llm, title, first_page, natives):
    """认不出印刷页码时的退路：从目录之后往后翻，找第一章的起始页。"""
    n = doc.page_count
    for a in range(first_page, min(first_page + 40, n + 1), 10):
        pages = list(range(a, min(a + 10, n + 1)))
        answer = llm.ask(PROMPT_START, pages_content(doc, pages, f"要找的章节：{title}", natives), 200)
        if to_int(answer.get("start_page")) in pages:
            return to_int(answer.get("start_page"))
    return None


def chapters_from_images(doc, llm, printed, natives, scan_pages, log):
    """扫描版没有书签时：大模型看图识别目录、核对各章起始页。返回 (章节列表, 印刷页码列表)。"""
    n = doc.page_count
    k = min(n, scan_pages)
    log(f"没有可用的书签，把前 {k} 页的图片发给大模型识别目录…")
    answer = llm.ask(PROMPT_TOC, pages_content(doc, range(1, k + 1), "这是一本书的前若干页。", natives), 8000)
    entries, body_start = toc_entries(answer, k)
    if len(entries) < 2:
        log("大模型没有在这些页里找到目录。")
        return [], printed
    log(f"目录里有 {len(entries)} 章，正在核对每一章的起始页…")

    if not any(printed):
        printed = probe_printed(doc, llm, natives)
    lookup = printed_lookup(printed)
    if lookup is None:
        first = find_first_chapter(doc, llm, entries[0][0], body_start, natives)
        if first is None:
            log("没能找到第一章从哪一页开始。")
            return [], printed
        shift = first - entries[0][1]
        lookup = lambda p: p + shift
    guesses = [lookup(page) for _, page in entries]

    def locate(windows):
        # 渲染放在主线程里做，PyMuPDF 的文档对象不能多线程共用
        contents = {
            i: pages_content(doc, pages, f"要找的章节：{entries[i][0]}", natives)
            for i, pages in windows.items() if pages
        }

        def one(i):
            try:
                page = to_int(llm.ask(PROMPT_START, contents[i], 200).get("start_page"))
            except LLMError:
                return i, None
            return i, page if page in windows[i] else None

        with ThreadPoolExecutor(6) as pool:
            return dict(pool.map(one, contents))

    def window(i, radius):
        return list(range(max(body_start, guesses[i] - radius), min(n, guesses[i] + radius) + 1))

    found = dict.fromkeys(range(len(entries)))
    found.update(locate({i: window(i, 2) for i in found}))
    missed = [i for i, page in found.items() if page is None]
    if missed:
        found.update(locate({i: window(i, 7) for i in missed}))
    return settle(entries, guesses, found, body_start, n, log), printed


# ---------------------------------------------------------------- 输出

def build_sections(chapters, n):
    if not chapters:
        return [Section("全书", "全书", 1, n)]
    parts = list(chapters)
    if parts[0].start > 1:
        parts.insert(0, Chapter("前置页", 1))
    width = max(2, len(str(len(parts) - 1)))
    sections = []
    for i, chapter in enumerate(parts):
        end = parts[i + 1].start - 1 if i + 1 < len(parts) else n
        folder = f"{i:0{width}d}_{safe_name(chapter.title)}"
        sections.append(Section(folder, chapter.title, chapter.start, end, chapter.checked))
    return sections


def span(values):
    values = [v for v in values if v]
    if not values:
        return "—"
    return str(values[0]) if values[0] == values[-1] else f"{values[0]}–{values[-1]}"


def write_index(path, title, pdf_name, kind, source, sections, printed, toc, args):
    n = len(printed)
    lines = [
        f"# {title}",
        "",
        f"- 源文件：{pdf_name}",
        f"- 总页数：{n}",
        f"- 类型：{kind}",
        f"- 章节来源：{source}",
        f"- 图片：长边不超过 {args.long_edge} 像素，{args.format} 格式",
        "- 文件名：前四位是 PDF 里的第几页，`_p` 后面是书上印刷的页码（认不出时省略）",
        "",
        "## 文件夹",
        "",
        "| 文件夹 | 页数 | 图片编号 | 印刷页码 |",
        "|---|---|---|---|",
    ]
    for s in sections:
        name = s.folder + ("" if s.checked else UNCHECKED)
        files = f"{s.start:04d}–{s.end:04d}" if s.end > s.start else f"{s.start:04d}"
        lines.append(f"| {name} | {s.end - s.start + 1} | {files} | {span(printed[s.start - 1:s.end])} |")
    if toc:
        lines += [
            "",
            "## 细目",
            "",
            "PDF 书签里的全部条目，后面是它所在的图片编号。小节常常在页面中间结束，",
            "所以小节的范围包含下一条目开始的那一页。",
            "",
        ]
        top = min(level for level, _, _ in toc)
        chapter_starts = {s.start for s in sections} | {n + 1}
        for i, (level, name, page) in enumerate(toc):
            following = next((p for lv, _, p in toc[i + 1:] if lv <= level), n + 1)
            end = max(page, following - 1 if following in chapter_starts else following)
            files = f"{page:04d}–{end:04d}" if end > page else f"{page:04d}"
            lines.append(f"{'  ' * (level - top)}- {name}　{files}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_index(path):
    """读回 write_index 写的 目录.md 里的概要和「文件夹」表。"""
    info = {"pages": 0, "kind": "", "source": "", "sections": []}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("- 总页数："):
            info["pages"] = to_int(line.split("：", 1)[1]) or 0
        elif line.startswith("- 类型："):
            info["kind"] = line.split("：", 1)[1]
        elif line.startswith("- 章节来源："):
            info["source"] = line.split("：", 1)[1]
        elif line.startswith("## 细目"):
            break
        elif line.startswith("| ") and not line.startswith("| 文件夹 |"):
            folder, pages, files, printed = (cell.strip() for cell in line.strip("|").split("|"))
            checked = not folder.endswith(UNCHECKED)
            info["sections"].append({
                "folder": folder if checked else folder[:-len(UNCHECKED)],
                "pages": to_int(pages) or 0, "files": files, "printed": printed, "checked": checked,
            })
    return info


def export_text(folder, section, stems, texts):
    chunks = [f"===== {stems[i]} =====\n{texts[i]}" for i in range(section.start - 1, section.end) if texts[i]]
    if sum(map(len, chunks)) > 200:
        (folder / TEXT_FILE).write_text("\n\n".join(chunks) + "\n", encoding="utf-8")


def book_dir(pdf, out_root=None):
    return (Path(out_root) if out_root else pdf.parent) / clean_title(pdf.stem)


def book_state(out):
    """new 还没分页 / done 分好了 / partial 上次没做完 / conflict 有同名文件夹但不是本工具生成的。"""
    if (out / MARK_BUSY).exists():
        return "partial"
    if (out / MARK_DONE).exists():
        return "done"
    return "conflict" if out.is_dir() and any(out.iterdir()) else "new"


def process(pdf, args, llm, pool, log, progress):
    """给一本书分页，返回 "done" 或 "skipped"。log(文字) 和 progress(已完成, 总数) 用来汇报进度。"""
    title = clean_title(pdf.stem)
    out = book_dir(pdf, args.out)
    state = book_state(out)
    if state == "partial" or (state == "done" and args.force):
        shutil.rmtree(out)
    elif state == "done":
        log("已经分过页，跳过。")
        return "skipped"
    elif state == "conflict":
        log(f"同名文件夹已存在，而且不是本工具生成的，跳过：{out}")
        return "skipped"
    started = time.time()
    doc = open_pdf(pdf)
    n = doc.page_count

    # 第一步：逐页检查，判断这本书是文字版还是扫描版
    texts, numbers, kinds, natives = inspect_all(pool, pdf, n)
    kind, has_text = book_kind(texts, kinds)
    label = KIND_LABELS[kind]
    log(f"共 {n} 页，{label}")
    cands = {i: found for i, found in enumerate(numbers) if found}
    printed = resolve_printed(cands, n) if has_text else [None] * n

    # 第二步：划分章节。有书签就用书签；没有的话文字版和扫描版各走各的流程
    raw_toc = [(lv, t.strip(), p) for lv, t, p in doc.get_toc() if 1 <= p <= n and t.strip()]
    chapters, source, toc = [], "无（未能分章）", []
    if not args.no_bookmarks:
        chapters = chapters_from_bookmarks(raw_toc, n)
    if chapters:
        source, toc = "PDF 书签", raw_toc
        log(f"从书签里读到 {len(chapters)} 个章节")
    try:
        if not chapters and not llm:
            log("没有可用的书签，也没有填 API Key，所有页面会放进同一个文件夹。")
        elif not chapters and kind == "text":
            chapters = chapters_from_text(doc, llm, texts, printed, args.toc_scan_pages, log)
            source = "大模型读目录文字，本地核对" if chapters else source
        elif not chapters:
            chapters, printed = chapters_from_images(doc, llm, printed, natives, args.toc_scan_pages, log)
            source = "大模型看图识别目录" if chapters else source
        elif llm and not any(printed) and kind != "text":
            printed = probe_printed(doc, llm, natives)
    except LLMError as err:
        log(f"调用大模型失败：{err}")

    # 第三步：导出。扫描页不会放大到超过原图的分辨率
    sections = build_sections(chapters, n)
    stems = [f"{i + 1:04d}" + (f"_p{printed[i]}" if printed[i] else "") for i in range(n)]
    out.mkdir(parents=True, exist_ok=True)
    (out / MARK_BUSY).touch()
    jobs = []
    for s in sections:
        (out / s.folder).mkdir(exist_ok=True)
        for i in range(s.start - 1, s.end):
            long_edge = min(args.long_edge, natives[i] or args.long_edge)
            jobs.append((i, str(out / s.folder / f"{stems[i]}.{args.format}"), long_edge))
    render_all(pool, pdf, jobs, args.format, args.quality, progress)
    if has_text and not args.no_text:
        for s in sections:
            export_text(out / s.folder, s, stems, texts)
    write_index(out / MARK_DONE, title, pdf.name, label, source, sections, printed, toc, args)
    (out / MARK_BUSY).unlink()
    doc.close()
    log(f"完成：{len(sections)} 个文件夹，用时 {time.time() - started:.0f} 秒")
    log(f"输出位置：{out}")
    return "done"


def load_config(path=None):
    config = dict(DEFAULT_CONFIG)
    path = Path(path) if path else APP_DIR / "config.json"
    if path.exists():
        config.update(json.loads(path.read_text(encoding="utf-8-sig")))
    config["api_key"] = (config.get("api_key") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    return config


def main():
    parser = argparse.ArgumentParser(description="把 PDF 电子书拆成逐页图片，并按章节分文件夹。")
    parser.add_argument("inputs", nargs="*", help="PDF 文件或文件夹；不填则处理本文件夹里的全部 PDF")
    parser.add_argument("--out", help="输出到哪个文件夹（默认放在 PDF 旁边）")
    parser.add_argument("--long-edge", type=int, default=1600, help="图片长边的像素数，默认 1600")
    parser.add_argument("--format", choices=["jpg", "png", "webp"], default="jpg", help="图片格式，默认 jpg")
    parser.add_argument("--quality", type=int, default=80, help="jpg/webp 的质量，默认 80")
    parser.add_argument("--no-text", action="store_true", help="不导出每章的文字")
    parser.add_argument("--no-bookmarks", action="store_true", help="忽略 PDF 书签，改用大模型识别目录")
    parser.add_argument("--no-api", action="store_true", help="不调用大模型")
    parser.add_argument("--force", action="store_true", help="重做已经分过页的书")
    args = parser.parse_args()

    sys.stdout.reconfigure(errors="replace")
    pymupdf.TOOLS.mupdf_display_errors(False)
    config = load_config()
    args.toc_scan_pages = int(config["toc_scan_pages"])
    llm = LLM(config) if config["api_key"] and not args.no_api else None

    pdfs = []
    for item in args.inputs or [APP_DIR]:
        path = Path(item)
        if path.is_dir():
            pdfs += sorted(path.glob("*.pdf"))
        elif path.suffix.lower() == ".pdf" and path.exists():
            pdfs.append(path)
        else:
            print(f"不是 PDF，跳过：{path}")
    if not pdfs:
        print("没有找到 PDF。把 PDF 放进本文件夹，或者把 PDF 拖到「拖入PDF开始分页.bat」上。")
        return

    def log(message):
        print("  " + message)

    def progress(done, total):
        print(f"\r  导出图片 {done}/{total}", end="\n" if done == total else "", flush=True)

    with ProcessPoolExecutor(min(16, os.cpu_count() or 1)) as pool:
        for pdf in pdfs:
            print(f"\n《{clean_title(pdf.stem)}》")
            try:
                process(pdf.resolve(), args, llm, pool, log, progress)
            except Exception as err:  # 一本书出错不影响后面的书
                print(f"  处理失败：{err}")
    if llm and llm.tokens_in:
        print(f"\n大模型用量：输入 {llm.tokens_in} tokens，输出 {llm.tokens_out} tokens")


if __name__ == "__main__":
    main()
