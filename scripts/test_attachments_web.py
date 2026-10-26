"""Attachments and web access (docs/features/attachments_and_web.md):
storage and endpoints, the tools over an attached long text (section reads, grep,
analyze, the per-turn cap), the web tools on a fake browser, the pure parts
of server_web, and a server run that reads an attachment."""
import asyncio
import json
from typing import Any, Dict, List

import pytest

import server_attachments
import server_generation
import server_runs
import server_web
from test_agent import FakeBook, run_tool
from test_runs import Scripted, book, request, settle  # noqa: F401 — `book` is a fixture
from wc_agent.tools.analyze_book import ATTACHMENT_CHUNK_CHARS
from wc_agent.tools.book_reads import list_tool, read_tool as analyze_book_tool  # read with a task (read_and_list.md)
from wc_agent.tools.book_reads import grep_tool, read_chapter_tool
from wc_agent.tools.web import web_read_tool, web_search_tool
from wc_text.attachments import ATTACHMENT_RUN_READ_CAP, attachment_paragraphs, attachment_sections
from wc_text.diff import strip_diff_markup
from wc_text.web_text import UNTRUSTED_WEB_NOTE

CN = "零一二三四五六七八九"


def cn_number(n: int) -> str:
    """1..99 in Chinese numerals, as chapter headings in a Chinese .txt write them."""
    if n < 10:
        return CN[n]
    tens, ones = divmod(n, 10)
    return f"{'' if tens == 1 else CN[tens]}十{CN[ones] if ones else ''}"


def long_text(chapters: int = 40, paragraphs: int = 50, width: int = 100) -> str:
    """A long text in the shape of a downloaded .txt: headings, then indented paragraphs."""
    out: List[str] = []
    for c in range(1, chapters + 1):
        out.append(f"第{cn_number(c)}章 第{c}回的故事")
        for p in range(1, paragraphs + 1):
            body = f"〔{c}-{p}〕" + ("林舟" if p == 7 else "") + "字" * width
            out.append("　　" + body[:width])
    return "\n".join(out)


class FakeAttachments:
    def __init__(self, items: Dict[str, str]) -> None:
        self.texts = {f"att-{i + 1}": t for i, t in enumerate(items.values())}
        self.meta = []
        for i, (name, text) in enumerate(items.items()):
            paras = attachment_paragraphs(text)
            self.meta.append({"id": f"att-{i + 1}", "ref": f"A{i + 1}", "name": name, "chars": sum(len(p) for p in paras),
                              "paragraphs": len(paras), "sections": attachment_sections(paras)})
        self.reads: List[str] = []
        self.pictures: Dict[str, Dict[int, Dict[str, Any]]] = {}

    def list(self):
        return self.meta

    async def paragraphs(self, attachment_id):
        self.reads.append(attachment_id)
        return attachment_paragraphs(self.texts[attachment_id])

    async def images(self, attachment_id):
        return self.pictures.get(attachment_id, {})

    async def save(self, name, text, images=None):
        attachment_id = f"att-{len(self.meta) + 1}"
        self.texts[attachment_id] = text
        self.pictures[attachment_id] = {i["n"]: i for i in images or []}
        paras = attachment_paragraphs(text)
        entry = {"id": attachment_id, "ref": f"A{len(self.meta) + 1}", "name": name, "chars": sum(len(p) for p in paras),
                 "paragraphs": len(paras), "sections": attachment_sections(paras)}
        self.meta.append(entry)
        return entry


def book_with_long_text() -> FakeBook:
    fake = FakeBook("<p>start</p>", chapters=[{"id": "doc-2", "title": "第二章", "content": "<p>林舟出场。</p>"}])
    fake.ctx.attachments = FakeAttachments({"雾港旧事.txt": long_text()})
    return fake


# ── reading an attachment ─────────────────────────────────────────────────────

def test_a_section_is_read_by_its_heading_in_either_numeral():
    fake = book_with_long_text()
    by_chinese = run_tool(read_chapter_tool, {"chapters": ["A1"], "section": "第三十章"}, fake.ctx)
    assert by_chinese["ok"], by_chinese["content"]
    assert "〔30-1〕" in by_chinese["content"] and "〔31-" not in by_chinese["content"]
    assert by_chinese["trace"].startswith('📖 read A1 "雾港旧事.txt" ¶')
    by_digits = run_tool(read_chapter_tool, {"chapters": ["附件1"], "section": "第30章"}, book_with_long_text().ctx)
    assert by_digits["content"] == by_chinese["content"]


def test_an_unknown_section_names_the_ones_there_are():
    out = run_tool(read_chapter_tool, {"chapters": ["A1"], "section": "第九十九章"}, book_with_long_text().ctx)
    assert not out["ok"]
    assert 'has no section matching "第九十九章"' in out["content"] and '"第一章 第1回的故事"' in out["content"]


def test_section_on_a_chapter_is_refused():
    out = run_tool(read_chapter_tool, {"chapters": ["2"], "section": "第一章"}, book_with_long_text().ctx)
    assert not out["ok"] and "names a part of an attachment" in out["content"]


def test_a_whole_attachment_is_never_read_in_one_go_and_the_turn_is_capped():
    fake = book_with_long_text()
    total = fake.ctx.attachments.meta[0]["chars"]
    assert total > 2 * ATTACHMENT_RUN_READ_CAP / 1.5  # the file is bigger than what one turn may read
    seen = 0
    start = 1
    for _ in range(20):
        out = run_tool(read_chapter_tool, {"chapters": ["A1"], "paragraphs": f"{start}-"}, fake.ctx)
        if not out["ok"]:
            break
        body = out["content"]
        assert len(body) < 21_000  # one read is one part, never the file
        last = max(int(line[1:].split(" ")[0]) for line in body.splitlines() if line.startswith("¶"))
        start = last + 1
        seen = fake.ctx.run.attachment_chars
    assert seen <= ATTACHMENT_RUN_READ_CAP
    assert not out["ok"] and "never read into the conversation" in out["content"] and 'grep chapters=["A1"]' in out["content"]


def test_grep_searches_an_attachment_only_when_named():
    fake = book_with_long_text()
    named = run_tool(grep_tool, {"pattern": "林舟", "chapters": ["A1"], "max_results": 3}, fake.ctx)
    assert "40 match(es) in 1 chapter(s) in 1 chapter(s); showing the first 3:" in named["content"]
    assert 'A1 "雾港旧事.txt" ¶8: ' in named["content"]
    assert named["trace"].startswith("🔎 grep /林舟/ in A1 → 40 matches")
    whole_book = run_tool(grep_tool, {"pattern": "林舟"}, fake.ctx)
    assert "A1" not in whole_book["content"] and '#2 "第二章" ¶1' in whole_book["content"]


def test_an_attachment_name_resolves_but_a_chapter_wins():
    fake = book_with_long_text()
    out = run_tool(read_chapter_tool, {"chapters": ["雾港旧事.txt"], "paragraphs": "1-2"}, fake.ctx)
    assert out["ok"] and "=== A1" in out["content"]
    out = run_tool(read_chapter_tool, {"chapters": ["第二章"]}, fake.ctx)
    assert "林舟出场" in out["content"]


def test_analyze_book_reads_an_attachment_in_chunks_outside_the_conversation():
    fake = book_with_long_text()
    seen: List[Dict[str, Any]] = []

    class Analyze:
        async def run(self, task, chapters, on_progress):
            seen.extend(chapters)
            return {"notes": "林舟在每一章都出现。", "batches": 2, "total": 2}

    fake.ctx.analyze = Analyze()
    out = run_tool(analyze_book_tool, {"task": "林舟的经历", "chapters": ["A1"]}, fake.ctx)
    assert out["ok"] and "林舟在每一章都出现" in out["content"]
    assert len(seen) == 40 and seen[0]["title"].startswith('A1 "雾港旧事.txt" — 第一章')
    assert all(len(c["content"]) <= ATTACHMENT_CHUNK_CHARS for c in seen)
    assert fake.ctx.run.attachment_chars == 0  # nothing of the file entered the conversation
    assert "A1" in out["trace"]


# ── web tools ─────────────────────────────────────────────────────────────────

class FakeWeb:
    def __init__(self, fail: str = "", pages: Dict[str, Dict[str, Any]] = None) -> None:
        self.fail = fail
        self.calls: List[Any] = []
        self.pages = pages or {}

    async def search(self, query, max_results):
        self.calls.append(("search", query, max_results))
        if self.fail:
            raise server_web.WebError(self.fail)
        return [{"title": "雾港旧事 - 百科", "url": "https://example.org/book-page", "snippet": "一部网络小说。"}]

    async def read(self, url):
        self.calls.append(("read", url))
        if self.fail:
            raise server_web.WebError(self.fail)
        if url in self.pages:
            return {"url": url, **self.pages[url]}
        return {"url": url, "title": "页面", "paragraphs": [f"第{i}段" for i in range(1, 31)]}

    async def images(self, url, numbers):
        self.calls.append(("images", url, numbers))
        if self.fail:
            raise server_web.WebError(self.fail)
        return [{"n": n, "alt": "封面" if n == 1 else "", "dataUrl": f"data:image/jpeg;base64,IMG{n}", "bytes": 40_000 * n} if n <= 2
                else {"n": n, "error": f"the page has no image {n} (it has 2)"} for n in numbers]


def test_web_images_register_placeholders_for_the_write():
    from wc_agent.tools.web import web_images_tool
    fake = book_with_long_text()
    web = FakeWeb()
    fake.ctx.web = web
    out = run_tool(web_images_tool, {"url": "https://example.org/post", "images": [1, 2, 2, 7]}, fake.ctx)
    assert out["ok"] and web.calls[0] == ("images", "https://example.org/post", [1, 2, 7])
    assert fake.ctx.images.registered == [{"dataUrl": "data:image/jpeg;base64,IMG1", "alt": "封面"}, {"dataUrl": "data:image/jpeg;base64,IMG2", "alt": ""}]
    assert "image 1 → {{IMAGE_PLACEHOLDER_0}} (封面), 39 KB" in out["content"] and "image 7: not fetched" in out["content"]
    assert out["trace"] == "🖼️ fetched 2/3 images from example.org"
    assert not run_tool(web_images_tool, {"url": "https://example.org/post", "images": []}, fake.ctx)["ok"]


def test_web_tools_are_offered_only_with_a_browser():
    fake = book_with_long_text()
    assert not web_search_tool.is_available(fake.ctx) and not web_read_tool.is_available(fake.ctx)
    fake.ctx.web = FakeWeb()
    assert web_search_tool.is_available(fake.ctx) and web_read_tool.is_available(fake.ctx)


def test_web_search_and_read_render_text_marked_untrusted():
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb()
    out = run_tool(web_search_tool, {"query": "雾港旧事 小说", "max_results": 50}, fake.ctx)
    assert out["ok"] and UNTRUSTED_WEB_NOTE in out["content"] and "https://example.org/book-page" in out["content"]
    assert fake.ctx.web.calls[0] == ("search", "雾港旧事 小说", 10)
    assert out["trace"] == '🌐 search "雾港旧事 小说" → 1 result'
    page = run_tool(web_read_tool, {"url": "https://example.org/book-page", "paragraphs": "5-6"}, fake.ctx)
    assert page["ok"] and "¶5 第5段\n¶6 第6段" in page["content"] and "¶7" not in page["content"]
    assert page["trace"] == "🌐 read example.org ¶5–6 (0.0k)"


def test_a_web_failure_reaches_the_model_as_a_reason():
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(fail="The search engine answered with a bot check; it was not bypassed.")
    out = run_tool(web_search_tool, {"query": "x"}, fake.ctx)
    assert not out["ok"] and "bot check" in out["content"] and out.get("retryable") is False


# ── server_web pure parts ─────────────────────────────────────────────────────

@pytest.mark.parametrize("ip,blocked", [("127.0.0.1", True), ("192.168.1.20", True), ("10.1.2.3", True), ("169.254.169.254", True),
                                        ("::1", True), ("0.0.0.0", True), ("8.8.8.8", False), ("2606:4700::1111", False)])
def test_private_and_local_addresses_are_blocked(ip, blocked):
    assert server_web.is_blocked_address(ip) is blocked


def test_the_user_agent_is_chromiums_own_without_headless():
    ua = server_web.chromium_user_agent("140.0.7339.16")
    assert "Chrome/140.0.0.0" in ua and "Headless" not in ua and "X11; Linux x86_64" in ua


def test_readable_paragraphs_drop_navigation_and_scripts():
    pytest.importorskip("bs4")
    html = """<html><head><title> 雾港旧事 </title><script>var x=1</script></head><body>
      <nav><a href="/">首页</a><a href="/a">分类</a></nav>
      <h1>第一章</h1><p>林舟睁开眼。</p><p>天亮了。</p><div><p>他起身。</p></div>
      <footer>版权所有</footer></body></html>"""
    title, paras, images = server_web.readable_paragraphs(html)
    assert title == "雾港旧事"
    assert paras == ["# 第一章", "林舟睁开眼。", "天亮了。", "他起身。"]
    assert images == []


def test_readable_paragraphs_number_the_images_where_they_sit():
    pytest.importorskip("bs4")
    html = """<html><body><h1>文章</h1><p>第一段。</p>
      <img src="/img/a.jpg" alt=" 封面 "><p>第二段。<img data-src="//cdn.example.org/b.webp"></p>
      <img data-lazy="https://site.example.org/c.png"><img src="/icons/like.png" width="16" height="16"><img src="/logo.svg">
      <img src="data:image/gif;base64,R0lG"><img src="/img/a.jpg"></body></html>"""
    _, paras, images = server_web.readable_paragraphs(html, "https://site.example.org/articles/1")
    assert paras == ["# 文章", "第一段。", "[image 1: 封面]", "第二段。", "[image 2]", "[image 3]"]
    assert images == [{"n": 1, "url": "https://site.example.org/img/a.jpg", "alt": "封面"}, {"n": 2, "url": "https://cdn.example.org/b.webp", "alt": ""},
                      {"n": 3, "url": "https://site.example.org/c.png", "alt": ""}]


def test_shrink_image_bounds_the_longest_side_and_flattens_alpha():
    Image = pytest.importorskip("PIL.Image")
    import io
    buf = io.BytesIO()
    Image.new("RGBA", (3000, 1500), (255, 0, 0, 128)).save(buf, "PNG")
    small, mime = server_web.shrink_image(buf.getvalue(), "image/png")
    assert mime == "image/jpeg" and len(small) < len(buf.getvalue())
    with Image.open(io.BytesIO(small)) as im:
        assert im.size == (1200, 600) and im.mode == "RGB"
    with pytest.raises(server_web.WebError):
        server_web.shrink_image(b"not an image" * 200_000, "application/octet-stream")


LITE_HTML = """<html><body><table>
<tr class="result-sponsored"><td><a class="result-link" href="https://ads.example/x">Ad</a></td></tr>
<tr class="result-sponsored"><td class="result-snippet">buy now</td></tr>
<tr><td><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fbook&rut=1">雾港旧事 小说</a></td></tr>
<tr><td class="result-snippet">  一部 网络小说  </td></tr>
<tr><td><a class="result-link" href="https://example.com/b">第二个</a></td></tr>
<tr><td class="result-snippet">second</td></tr>
</table></body></html>"""


def test_search_results_are_parsed_with_ads_skipped_and_redirects_unwrapped():
    pytest.importorskip("bs4")
    results = server_web.parse_search_results(LITE_HTML, 8)
    assert results == [{"title": "雾港旧事 小说", "url": "https://example.org/book", "snippet": "一部 网络小说"},
                       {"title": "第二个", "url": "https://example.com/b", "snippet": "second"}]
    assert len(server_web.parse_search_results(LITE_HTML, 1)) == 1


def test_a_bot_check_is_recognized():
    assert server_web.is_challenge_page('<div class="anomaly-modal">x</div>')
    assert not server_web.is_challenge_page(LITE_HTML)


def test_a_site_bot_check_is_told_from_a_page_that_mentions_one():
    assert server_web.is_bot_check("<title>Just a moment...</title><div id=challenge-platform></div>", ["Just a moment..."])
    assert not server_web.is_bot_check("<p>an ordinary page</p>", ["an ordinary page"])
    # A long article that embeds a captcha in its comment form is the page, not a check.
    assert not server_web.is_bot_check('<div class="g-recaptcha"></div>', ["正文" * 1000])


def test_page_links_are_absolute_deduplicated_and_marked_same_site():
    pytest.importorskip("bs4")
    html = ('<a href="/c1">第一章</a><a href="c1#top">again</a><a href="https://m.book.example.com/c2">第二章</a>'
            '<a href="https://other.example.net/x">外站</a><a href="javascript:void(0)">js</a><a href="mailto:a@b.c">mail</a><a href="#">top</a>')
    links = server_web.page_links(html, "https://book.example.com/index.html")
    assert [(l["url"], l["text"], l["sameSite"]) for l in links] == [
        ("https://book.example.com/c1", "第一章", True), ("https://m.book.example.com/c2", "第二章", True), ("https://other.example.net/x", "外站", False)]
    assert [l["n"] for l in links] == [1, 2, 3]
    # On a table of contents, the site menu goes behind the chapter links.
    toc = server_web.page_links('<nav><a href="/">首页</a><a href="/top">排行</a></nav><ul><li><a href="/c1">第一章</a></li></ul>',
                                "https://book.example.com/b/")
    assert [(l["n"], l["text"], l["chrome"]) for l in toc] == [(1, "第一章", False), (2, "首页", True), (3, "排行", True)]


def test_a_long_link_list_is_paged():
    from wc_agent.tools.web import LISTED_LINKS, render_links
    page = {"links": [{"n": i, "url": f"https://site.example.com/{i}", "text": f"第{i}章", "sameSite": True} for i in range(1, 151)]}
    first = render_links(page, "site")
    assert f"L{LISTED_LINKS} 第{LISTED_LINKS}章" in first and f"L{LISTED_LINKS + 1} " not in first
    assert f"pass links_from={LISTED_LINKS + 1}" in first
    last = render_links(page, "site", 121)
    assert "L121 第121章" in last and "L150 第150章" in last and "links_from" not in last


def test_visits_to_one_host_are_spaced_but_other_hosts_are_not(monkeypatch):
    clock = {"t": 100.0}
    slept: List[float] = []

    async def fake_sleep(seconds):
        slept.append(round(seconds, 3))
        clock["t"] += seconds
    monkeypatch.setattr(server_web.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(server_web.asyncio, "sleep", fake_sleep)
    browser = server_web._Browser()

    async def visits():
        await browser.host_turn("a.example")
        clock["t"] += 0.25
        await browser.host_turn("a.example")
        await browser.host_turn("b.example")
    asyncio.run(visits())
    assert slept == [server_web.HOST_INTERVAL_SECONDS - 0.25]


# ── storage and endpoints ─────────────────────────────────────────────────────

def test_decode_text_handles_the_encodings_chinese_txt_files_come_in():
    text = "第一章 开端\n　　林舟睁开眼。"
    assert server_attachments.decode_text(text.encode("utf-8")) == text
    assert server_attachments.decode_text(b"\xef\xbb\xbf" + text.encode("utf-8")) == text
    assert server_attachments.decode_text(text.encode("gb18030")) == text
    assert server_attachments.decode_text("第一章 開端\n　　林舟睜開眼。".encode("big5")) == "第一章 開端\n　　林舟睜開眼。"


def test_upload_list_read_and_delete_through_the_endpoints(book):  # noqa: F811
    from fastapi.testclient import TestClient
    import api_server

    client = TestClient(api_server.app)
    client.cookies.update({"web_canvas_session": "sess-1", "csrf_token": "tok-1"})
    headers = {"x-csrf-token": "tok-1", "content-type": "application/octet-stream"}
    raw = long_text(3, 4, 20).encode("gb18030")
    res = client.post("/api/books/book-1/attachments?name=雾港旧事.txt", content=raw, headers=headers)
    assert res.status_code == 200, res.text
    meta = res.json()["attachment"]
    assert meta["ref"] == "A1" and meta["name"] == "雾港旧事.txt" and meta["paragraphs"] == 15 and len(meta["sections"]) == 3

    second = client.post("/api/books/book-1/attachments?name=notes.md", content="# 设定\n\n林舟：主角".encode(), headers=headers).json()["attachment"]
    assert second["ref"] == "A2"
    listed = client.get("/api/books/book-1/attachments").json()["attachments"]
    assert [a["ref"] for a in listed] == ["A1", "A2"]

    text = client.get(f"/api/books/book-1/attachments/{meta['id']}/text").json()["text"]
    assert text.startswith("第一章 第1回的故事") and len(attachment_paragraphs(text)) == 15

    assert client.post("/api/books/book-1/attachments?name=x.pdf", content=b"%PDF", headers=headers).status_code == 400
    assert client.post("/api/books/nope/attachments?name=x.txt", content=b"abc", headers=headers).status_code == 404
    assert client.post("/api/books/book-1/attachments?name=x.txt", content=b"  \n", headers=headers).status_code == 400

    assert client.delete(f"/api/books/book-1/attachments/{meta['id']}", headers={"x-csrf-token": "tok-1"}).status_code == 200
    listed = client.get("/api/books/book-1/attachments").json()["attachments"]
    assert [(a["ref"], a["name"]) for a in listed] == [("A1", "notes.md")]
    assert client.get(f"/api/books/book-1/attachments/{meta['id']}/text").status_code == 404


# ── a server run ──────────────────────────────────────────────────────────────

def test_a_server_run_lists_and_reads_an_attachment_but_never_sends_it_whole(book, monkeypatch):  # noqa: F811
    server_attachments.store_attachment("alice", "book-1", "雾港旧事.txt", long_text().encode("utf-8"))
    monkeypatch.setattr(server_web, "available", lambda: False)
    provider = Scripted([
        {"text": "", "calls": [("c1", "read_chapter", json.dumps({"chapters": ["A1"], "section": "第三十章"}, ensure_ascii=False))]},
        "第三十章讲的是第30回的故事。\n<doc_status>unchanged</doc_status>",
    ])
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", request("第三十章讲了什么？"))
        await settle(run)
        return run
    run = asyncio.run(main())
    assert run.status == "done", run.error
    first = provider.requests[0]
    last_user = first["messages"][-1]["content"]
    assert 'ATTACHMENTS' in last_user and 'A1 "雾港旧事.txt"' in last_user
    whole = json.dumps(first["messages"], ensure_ascii=False)
    assert "〔30-1〕" not in whole and len(whole) < 60_000
    tools = [t.get("name") or t.get("function", {}).get("name") for t in (first["config"].get("tools") or [])]
    assert "read" in tools and "list" in tools and "read_chapter" not in tools and "web_search" not in tools
    second = json.dumps(provider.requests[1]["messages"], ensure_ascii=False)
    assert "〔30-1〕" in second and "〔31-1〕" not in second


# ── analyze_book on part of an attachment, and what it costs ─────────────────

class _AnalyzeSpy:
    def __init__(self, plan=None):
        self.seen = []
        self._plan = plan

    async def run(self, task, chapters, on_progress):
        self.seen.extend(chapters)
        return {"notes": "笔记。", "batches": 1, "total": 1, "stopped": False}


class _AnalyzeSpyWithPlan(_AnalyzeSpy):
    def plan(self, task, chapters):
        return self._plan


def test_analyze_reads_only_the_sections_or_paragraphs_named():
    fake = book_with_long_text()
    spy = _AnalyzeSpy()
    fake.ctx.analyze = spy
    # Eleven sections (~55k characters): more than one read, so it is read for the task.
    out = run_tool(analyze_book_tool, {"task": "t", "chapters": ["A1"], "section": "第十–二十章"}, fake.ctx)
    assert out["ok"] and "A1 第十–二十章" in out["trace"]
    titles = [c["title"].split(" — ")[1].split(" (")[0] for c in spy.seen]
    assert titles[0] == "第十章 第10回的故事" and titles[-1] == "第二十章 第20回的故事" and len(titles) == 11
    spy2 = _AnalyzeSpy()
    fake.ctx.analyze = spy2
    out = run_tool(analyze_book_tool, {"task": "t", "chapters": ["A1"], "paragraphs": "52-500"}, fake.ctx)
    text = "\n".join(c["content"] for c in spy2.seen)
    assert out["ok"] and "A1 ¶52–500" in out["trace"] and "〔2-1〕" in text and "〔1-50〕" not in text and "〔11-1〕" not in text
    assert "has no section matching" in run_tool(analyze_book_tool, {"task": "t", "chapters": ["A1"], "section": "第九十九章"}, fake.ctx)["content"]
    assert "names a part of an attachment" in run_tool(analyze_book_tool, {"task": "t", "chapters": ["2"], "section": "第一章"}, fake.ctx)["content"]


def test_analyze_asks_first_past_the_token_line():
    fake = book_with_long_text()
    spy = _AnalyzeSpyWithPlan({"calls": 11, "inputTokens": 1_500_000, "batchChars": 140_000})
    fake.ctx.analyze = spy
    first = run_tool(analyze_book_tool, {"task": "t", "chapters": ["A1"]}, fake.ctx)
    assert not first["ok"] and "1500000 input tokens in 11 model calls" in first["content"] and "ask_user" in first["content"]
    assert first["trace"] == "📚 read for a task: A1 ≈ 1500k tokens in 11 calls — asks first" and spy.seen == []
    assert run_tool(analyze_book_tool, {"task": "t", "chapters": ["A1"], "confirmed": True}, fake.ctx)["ok"] and spy.seen


def test_batches_stay_under_groks_price_line_for_chinese_text():
    from wc_agent.polish import ANALYZE_CONFIRM_TOKENS, plan_analysis
    chapters = [{"id": f"c{i}", "title": f"第{i}章", "content": "字" * 100_000} for i in range(20)]
    plan = plan_analysis("t", chapters, "grok")
    assert plan["batchChars"] == 140_000 and plan["calls"] == 20
    assert plan["inputTokens"] > ANALYZE_CONFIRM_TOKENS and plan["inputTokens"] / plan["calls"] < 200_000



# ── list (read_and_list.md §3) ────────────────────────────────────────────────

def test_list_shows_chapters_and_attachments_then_an_attachments_sections():
    fake = book_with_long_text()
    out = run_tool(list_tool, {}, fake.ctx)
    assert '#2 "第二章"' in out["content"] and 'A1 "雾港旧事.txt" — ' in out["content"] and "40 sections" in out["content"]
    assert out["trace"] == "📚 list chapters and 1 attachment"
    page = run_tool(list_tool, {"source": "A1", "from": 39}, fake.ctx)
    assert "39. ¶1939–1989 第三十九章 第39回的故事" in page["content"] and page["trace"] == "📚 list A1 sections 39–40 of 40"
    run = run_tool(list_tool, {"source": "A1", "section": "第十–十二章"}, fake.ctx)
    assert "1. ¶460–510 第十章 第10回的故事" in run["content"] and "第十三章" not in run["content"]
    assert run_tool(list_tool, {"source": "A1", "paragraphs": "1-60"}, fake.ctx)["trace"] == "📚 list A1 sections 1–2 of 2"


def test_list_shows_a_chapters_headings_and_answers_to_list_chapters():
    from wc_agent.registry import ToolRegistry
    from wc_agent.tools.book_reads import read_tool
    fake = FakeBook("<p>start</p>", chapters=[{"id": "doc-2", "title": "大纲", "content": "<h1>大纲</h1><p>a</p><h2>第一卷</h2><p>b</p>"}])
    assert "¶1 # 大纲\n¶3 # 第一卷" in run_tool(list_tool, {"source": "2"}, fake.ctx)["content"]
    registry = ToolRegistry([read_tool, list_tool])
    assert registry.get("list_chapters") is list_tool and registry.get("read_chapter") is read_tool and registry.get("analyze_book") is read_tool


# ── read with a task (read_and_list.md §2) ────────────────────────────────────

def _book_with_long_chapter() -> FakeBook:
    long = "".join(f"<p>{'长' * 1000}{i}</p>" for i in range(30))
    return FakeBook("<p>序章。</p>", chapters=[{"id": "doc-2", "title": "第一章", "content": "<p>一</p>"}, {"id": "doc-3", "title": "第三章", "content": long}])


def test_read_with_a_task_returns_text_when_it_fits_and_notes_when_it_does_not():
    fake = _book_with_long_chapter()
    spy = _AnalyzeSpy()
    fake.ctx.analyze = spy
    small = run_tool(analyze_book_tool, {"task": "t", "chapters": ["2"]}, fake.ctx)
    assert spy.seen == [] and "¶1 一" in small["content"]
    big = run_tool(analyze_book_tool, {"task": "t", "chapters": ["3"], "paragraphs": "1-25"}, fake.ctx)
    assert [c["title"] for c in spy.seen] == ["第三章 (¶1–25)"] and big["trace"] == "📚 read #3 ¶1–25 for a task — 1 part, 1 model call"
    whole = run_tool(analyze_book_tool, {"task": "t"}, fake.ctx)
    assert whole["trace"].startswith("📚 read the book for a task — 3 parts")


def test_a_cut_read_says_what_the_rest_would_cost():
    fake = _book_with_long_chapter()
    fake.ctx.analyze = _AnalyzeSpyWithPlan({"calls": 1, "inputTokens": 31_000, "batchChars": 140_000})
    out = run_tool(analyze_book_tool, {"chapters": ["3"]}, fake.ctx)
    assert out["ok"] and "Continue with chapters=[3]" in out["content"]
    assert ('[Not all of it fit in one read. To have all of it read and get notes, call read again with the same arguments and task="what the notes are for": '
            "1 batch outside the conversation, ≈31k input tokens.]") in out["content"]


def test_a_task_read_without_a_model_says_to_read_in_parts():
    fake = _book_with_long_chapter()
    out = run_tool(analyze_book_tool, {"task": "t"}, fake.ctx)
    assert not out["ok"] and "read it part by part" in out["content"]


# ── exploring links, saving pages (attachments_and_web.md §3) ─────────────────

TOC = {"title": "目录", "paragraphs": ["# 雾港旧事", "作者：某某"],
       "links": [{"n": 1, "url": "https://book.example.com/c1", "text": "第一章 开局", "sameSite": True},
                 {"n": 2, "url": "https://ads.example.net/x", "text": "广告", "sameSite": False},
                 {"n": 3, "url": "https://book.example.com/c2", "text": "第二章 重逢", "sameSite": True}]}
C1 = {"title": "第一章 开局", "paragraphs": ["# 第一章 开局", "林舟醒来。", "[image 1: 地图]", "他看见了天。"], "links": []}
C2 = {"title": "第二章 重逢", "paragraphs": ["# 第二章 重逢", "船笛响了。"], "links": []}
PAGES = {"https://book.example.com/": TOC, "https://book.example.com/c1": C1, "https://book.example.com/c2": C2}


def test_web_read_lists_same_site_links_and_reads_several_pages():
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(pages=PAGES)
    out = run_tool(web_read_tool, {"url": "https://book.example.com/"}, fake.ctx)
    assert out["ok"]
    assert "L1 第一章 开局 — https://book.example.com/c1" in out["content"] and "L3 第二章 重逢" in out["content"]
    assert "ads.example.net" not in out["content"] and "1 links to other sites not listed" in out["content"]
    every = run_tool(web_read_tool, {"url": "https://book.example.com/", "links": "all"}, fake.ctx)
    assert "L2 广告 — https://ads.example.net/x" in every["content"]
    both = run_tool(web_read_tool, {"urls": ["https://book.example.com/c1", "https://book.example.com/c2"]}, fake.ctx)
    assert both["ok"] and "林舟醒来" in both["content"] and "船笛响了" in both["content"]
    assert both["trace"].startswith("🌐 read book.example.com") and both["trace"].count("book.example.com") == 2
    assert not run_tool(web_read_tool, {"urls": [f"https://site.example.com/{i}" for i in range(6)]}, fake.ctx)["ok"]


def test_web_read_reports_one_failed_page_among_several():
    fake = book_with_long_text()

    class Flaky(FakeWeb):
        async def read(self, url):
            if url.endswith("/c2"):
                raise server_web.WebError("book.example.com answered with a bot check (a verification page) instead of the page; it was not bypassed.")
            return await super().read(url)
    fake.ctx.web = Flaky(pages=PAGES)
    out = run_tool(web_read_tool, {"urls": ["https://book.example.com/c1", "https://book.example.com/c2"]}, fake.ctx)
    assert out["ok"] and "林舟醒来" in out["content"] and "bot check" in out["content"] and "not bypassed" in out["content"]


def test_web_save_snapshots_pages_into_one_attachment_by_default():
    from wc_agent.tools.web_save import web_save_tool
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(pages=PAGES)
    out = run_tool(web_save_tool, {"urls": ["https://book.example.com/c1", "https://book.example.com/c2"], "name": "雾港旧事 全文"}, fake.ctx)
    assert out["ok"], out["content"]
    assert 'attachment A2 "雾港旧事 全文"' in out["content"] and 'copy_paragraphs from_chapter="A2"' in out["content"]
    assert fake.writes == []  # no chapter touched
    paras = attachment_paragraphs(fake.ctx.attachments.texts["att-2"])
    assert paras[:6] == ["第一章 开局", "Source: https://book.example.com/c1", "第一章 开局", "林舟醒来。", "[image 1: 地图]", "他看见了天。"]
    # The page's picture is kept beside the text, under the attachment's own number.
    assert fake.ctx.attachments.pictures["att-2"][1]["dataUrl"] == "data:image/jpeg;base64,IMG1"
    # The receipt's spans are the attachment's own ¶ numbers.
    assert "¶1–6: 第一章 开局" in out["content"] and "¶7–10: 第二章 重逢" in out["content"] and paras[6] == "第二章 重逢"
    assert fake.ctx.attachments.meta[1]["sections"][0]["title"] == "第一章 开局"
    assert out["trace"] == "💾 saved 2 page(s) → A2 雾港旧事 全文, 1 image(s)"
    assert 'view_images chapter="A2"' in out["content"]


def test_web_save_into_a_chapter_inlines_the_pictures_after_a_given_chapter():
    from wc_agent.tools.web_save import web_save_tool
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(pages=PAGES)
    out = run_tool(web_save_tool, {"url": "https://book.example.com/c1", "paragraphs": "2-4", "after_chapter": 0, "new_chapter": "摘录 第一章"}, fake.ctx)
    assert out["ok"], out["content"]
    assert fake.book[0]["title"] == "摘录 第一章"
    written = strip_diff_markup(fake.last_write("new-1"))
    assert written.startswith("<p>林舟醒来。</p>") and '<img src="data:image/jpeg;base64,IMG1" alt="封面">' in written
    assert 'Source: <a href="https://book.example.com/c1">' in written and "第一章 开局" not in written
    assert out["trace"] == "💾 saved book.example.com ¶2–4 → new #1 \"摘录 第一章\", 1 image(s)"


def test_web_save_into_an_existing_chapter_after_a_paragraph():
    from wc_agent.tools.web_save import web_save_tool
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(pages=PAGES)
    wrong = run_tool(web_save_tool, {"url": "https://book.example.com/c2", "to_chapter": "2", "after_paragraph": 1, "after_starts_with": "别的"}, fake.ctx)
    assert not wrong["ok"] and fake.writes == []
    out = run_tool(web_save_tool, {"url": "https://book.example.com/c2", "to_chapter": "2", "after_paragraph": 1,
                                   "after_starts_with": "林舟出场", "source_link": False}, fake.ctx)
    assert out["ok"], out["content"]
    assert strip_diff_markup(fake.last_write("doc-2")) == "<p>林舟出场。</p><h2>第二章 重逢</h2><p>船笛响了。</p>"


def test_copy_paragraphs_copies_from_an_attachment_and_leaves_it_alone():
    from wc_agent.tools.paragraph_moves import copy_paragraphs_tool
    from wc_agent.tools.web_save import web_save_tool
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(pages=PAGES)
    run_tool(web_save_tool, {"urls": ["https://book.example.com/c1"]}, fake.ctx)
    before = fake.ctx.attachments.texts["att-2"]
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "A2", "paragraphs": "4-6", "starts_with": "林舟醒来", "last_starts_with": "他看见",
                                          "new_chapter": "摘录 第一章", "after_chapter": 1}, fake.ctx)
    assert out["ok"], out["content"]
    assert [c["title"] for c in fake.book][:2][1] == "摘录 第一章"
    assert strip_diff_markup(fake.last_write("new-1")) == '<p>林舟醒来。</p><p><img src="data:image/jpeg;base64,IMG1" alt="封面"></p><p>他看见了天。</p>'
    assert "1 picture(s)" in out["content"]
    assert fake.ctx.attachments.texts["att-2"] == before and "attachments are never written" in out["content"]
    wrong = run_tool(copy_paragraphs_tool, {"from_chapter": "A2", "paragraphs": "4", "starts_with": "别的", "to_chapter": "2"}, fake.ctx)
    assert not wrong["ok"] and "Nothing was changed" in wrong["content"]


def test_a_saved_page_named_like_a_chapter_does_not_shadow_it():
    from wc_agent.tools.paragraph_moves import copy_paragraphs_tool
    fake = book_with_long_text()
    fake.ctx.attachments.meta[0]["name"] = "第二章"
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "第二章", "paragraphs": "1", "starts_with": "林舟出场", "new_chapter": "副本", "move": False}, fake.ctx)
    assert out["ok"] and "attachment" not in out["content"]


def test_images_across_saved_pages_are_numbered_through_the_attachment():
    from wc_agent.tools.web_save import web_save_tool
    pages = {"https://site.example.com/1": {"title": "一", "paragraphs": ["甲", "[image 2: 二]"]},
             "https://site.example.com/2": {"title": "二", "paragraphs": ["[image 1]", "乙", "[image 9: 坏]"]}}
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(pages=pages)
    out = run_tool(web_save_tool, {"urls": list(pages)}, fake.ctx)
    paras = attachment_paragraphs(fake.ctx.attachments.texts["att-2"])
    assert paras == ["一", "Source: https://site.example.com/1", "甲", "[image 1: 二]", "二", "Source: https://site.example.com/2", "[image 2]", "乙", "[image not saved: 坏]"]
    assert [i["dataUrl"][-4:] for i in fake.ctx.attachments.pictures["att-2"].values()] == ["IMG2", "IMG1"]
    assert "1 picture(s) could not be fetched" in out["content"]
    text_only = run_tool(web_save_tool, {"urls": list(pages), "images": False, "name": "纯文字"}, fake.ctx)
    assert text_only["ok"] and "[image not saved: 二]" in fake.ctx.attachments.texts["att-3"] and fake.ctx.attachments.pictures["att-3"] == {}


def test_view_images_shows_an_attachments_saved_pictures():
    from wc_agent.tools.view_images import view_images_tool
    from wc_agent.tools.web_save import web_save_tool
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(pages=PAGES)
    fake.ctx.vision = object()
    run_tool(web_save_tool, {"url": "https://book.example.com/c1"}, fake.ctx)
    out = run_tool(view_images_tool, {"chapter": "A2"}, fake.ctx)
    assert out["ok"], out["content"]
    assert out["images"] == ["data:image/jpeg;base64,IMG1"] and "in ¶5" in out["content"] and "IMAGE_PLACEHOLDER" in out["content"]
    assert not run_tool(view_images_tool, {"chapter": "A1"}, fake.ctx)["ok"]  # an uploaded file has none


def test_web_images_can_show_what_it_fetched():
    from wc_agent.tools.web import web_images_tool
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb()
    blind = run_tool(web_images_tool, {"url": "https://example.org/post", "images": [1], "view": True}, fake.ctx)
    assert "images" not in blind and "does not take images" in blind["content"]
    fake.ctx.vision = object()
    out = run_tool(web_images_tool, {"url": "https://example.org/post", "images": [1, 2], "view": True}, fake.ctx)
    assert out["images"] == ["data:image/jpeg;base64,IMG1", "data:image/jpeg;base64,IMG2"]
    assert "[Image 1] = image 1, [Image 2] = image 2" in out["content"] and fake.ctx.run.images_viewed == 2
    plain = run_tool(web_images_tool, {"url": "https://example.org/post", "images": [1]}, fake.ctx)
    assert "images" not in plain


def test_decoration_is_not_listed_as_a_pages_picture():
    pytest.importorskip("bs4")
    html = ('<body><img src="templates/basic/images/logo.png"><img src="https://ads.example.net/ad.gif">'
            '<img src="images/default/star_level2.gif"><img src="images/smilies/default/lol.gif"><img src="images/default/attachimg.gif">'
            '<p>正文一段</p><p>正文二段</p><p>正文三段</p>'
            '<img src="images/default/attachimg.gif" file="https://img.example.com/attachments/1.jpg" alt="IMG_1.JPG">'
            '<img src="https://img.example.com/attachments/2.jpg"><img src="https://img.example.com/big.gif" width="480"></body>')
    _, paragraphs, images = server_web.readable_paragraphs(html, "https://site.example.com/page?id=1")
    assert [i["url"] for i in images] == ["https://img.example.com/attachments/1.jpg", "https://img.example.com/attachments/2.jpg", "https://img.example.com/big.gif"]
    assert "[image 1: IMG_1.JPG]" in paragraphs


def test_a_saved_pages_pictures_are_stored_and_removed_with_it(book):  # noqa: F811
    import os
    pics = [{"n": 1, "alt": "图", "dataUrl": "data:image/jpeg;base64,AAA", "src": "https://site.example.com/1.jpg"}]
    saved = server_attachments.store_attachment("alice", "book-1", "页面", "甲\n\n[image 1: 图]\n".encode(), pics)
    assert server_attachments.attachment_images("alice", "book-1", saved["id"])[1]["dataUrl"] == "data:image/jpeg;base64,AAA"
    sidecar = server_attachments._images_path("alice", "book-1", saved["id"])
    assert os.path.exists(sidecar)
    assert server_attachments.delete_attachment("alice", "book-1", saved["id"]) and not os.path.exists(sidecar)
    plain = server_attachments.store_attachment("alice", "book-1", "上传.txt", "乙".encode())
    assert server_attachments.attachment_images("alice", "book-1", plain["id"]) == {}


def test_a_page_the_user_linked_is_kept_as_an_attachment_and_its_pictures_shown():
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(pages=PAGES)
    fake.ctx.vision = object()
    fake.ctx.run.user_urls = ["https://book.example.com/c1/#top"]  # as the user pasted it
    out = run_tool(web_read_tool, {"url": "https://book.example.com/c1"}, fake.ctx)
    assert out["ok"] and 'kept in the book as attachment A2 "第一章 开局" with its 1 picture(s)' in out["content"]
    assert out["images"] == ["data:image/jpeg;base64,IMG1"] and "[Image 1] = A2 image 1 (this page's [image 1])" in out["content"]
    assert out["trace"] == "🌐 read book.example.com (0.0k) → saved as A2 with 1 picture(s), viewed 1 image(s)" and fake.ctx.run.images_viewed == 1
    assert attachment_paragraphs(fake.ctx.attachments.texts["att-2"])[4] == "[image 1: 地图]"
    # Read again (this turn or a later one): the same attachment, not a second copy.
    again = run_tool(web_read_tool, {"url": "https://book.example.com/c1"}, fake.ctx)
    assert "already kept in the book as attachment A2" in again["content"] and len(fake.ctx.attachments.meta) == 2
    assert "→ already in A2 with 1 picture(s)" in again["trace"]
    # A page the model found by itself is only read.
    other = run_tool(web_read_tool, {"url": "https://book.example.com/c2"}, fake.ctx)
    assert "kept in the book" not in other["content"] and "images" not in other and len(fake.ctx.attachments.meta) == 2


def test_a_linked_page_with_many_pictures_shows_the_first_and_says_how_to_see_the_rest():
    many = {"title": "附图页", "paragraphs": ["正文"] + [f"[image {n}]" for n in range(1, 101)]}

    class PictureWeb(FakeWeb):
        async def images(self, url, numbers):
            self.calls.append(("images", url, numbers))
            return [{"n": n, "alt": "", "dataUrl": f"data:image/jpeg;base64,P{n}"} for n in numbers]
    fake = book_with_long_text()
    fake.ctx.web = PictureWeb(pages={"https://site.example.com/pictures": many})
    fake.ctx.vision = object()
    fake.ctx.run.user_urls = ["https://site.example.com/pictures"]
    out = run_tool(web_read_tool, {"url": "https://site.example.com/pictures"}, fake.ctx)
    assert len(fake.ctx.attachments.pictures["att-2"]) == 100
    assert [c[2][0] for c in fake.ctx.web.calls if c[0] == "images"] == [1, 21, 41, 61, 81]  # 20 per fetch
    assert len(out["images"]) == 6 and '94 more picture(s) not shown here: view_images chapter="A2"' in out["content"]
    assert any("saving pictures 21–40 of 100" in (line or "") for line in fake.progress)


def test_without_vision_a_linked_page_is_kept_but_nothing_is_shown():
    fake = book_with_long_text()
    fake.ctx.web = FakeWeb(pages=PAGES)
    fake.ctx.run.user_urls = ["https://book.example.com/c1"]
    out = run_tool(web_read_tool, {"url": "https://book.example.com/c1"}, fake.ctx)
    assert "attachment A2" in out["content"] and "images" not in out and "1 more picture(s) not shown" in out["content"]


def test_the_addresses_a_message_names_are_found():
    from wc_agent.types import urls_in_text
    assert urls_in_text("https://site.example.com/page?id=1001 请读一下这个页面，另见 https://site.example.com/b。还有https://site.example.com/b") == [
        "https://site.example.com/page?id=1001", "https://site.example.com/b"]



class FakeLook:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def look(self, task, items, on_progress):
        self.calls.append((task, [i["label"] for i in items]))
        on_progress(0, 1)
        on_progress(1, 1)
        return {"lines": [f"{i['label']}: 一张图" for i in items], "batches": (len(items) + 7) // 8, "total": (len(items) + 7) // 8, "stopped": False}


def test_view_images_with_a_task_looks_at_every_picture_outside_the_conversation():
    from wc_agent.tools.view_images import view_images_tool
    many = {"title": "附图页", "paragraphs": ["正文"] + [f"[image {n}]" for n in range(1, 41)]}

    class PictureWeb(FakeWeb):
        async def images(self, url, numbers):
            return [{"n": n, "alt": "", "dataUrl": f"data:image/jpeg;base64,P{n}"} for n in numbers]
    fake = book_with_long_text()
    fake.ctx.web = PictureWeb(pages={"https://site.example.com/pictures": many})
    fake.ctx.vision = object()
    fake.ctx.run.user_urls = ["https://site.example.com/pictures"]
    run_tool(web_read_tool, {"url": "https://site.example.com/pictures"}, fake.ctx)
    assert fake.ctx.run.images_viewed == 6
    fake.ctx.run.images_viewed = 30  # the turn's in-conversation limit is spent: a task is not held by it
    fake.ctx.analyze = FakeLook()
    out = run_tool(view_images_tool, {"chapter": "A2", "task": "描述每张图片"}, fake.ctx)
    assert out["ok"], out["content"]
    assert "images" not in out  # nothing enters the conversation but the lines
    task, labels = fake.ctx.analyze.calls[0]
    assert task == "描述每张图片" and len(labels) == 40 and labels[0] == "image 1 (¶4)"
    assert "Looked at 40 of A2" in out["content"] and "image 40 (¶43): 一张图" in out["content"]
    assert "image 1 (¶4) — token {{IMAGE_PLACEHOLDER_" in out["content"]
    assert out["trace"] == '🖼️ looked at 40 pictures of A2 "附图页" (5 batch(es))'


def test_view_images_with_a_task_on_a_chapter_loads_its_pictures_first():
    from wc_agent.tools.view_images import view_images_tool
    imgs = "".join(f'<p>段{n}</p><p><img src="https://img.example.com/{n}.jpg"></p>' for n in range(1, 4))
    fake = FakeBook(imgs)
    loaded: List[Any] = []

    class Vision:
        async def view(self, srcs):
            loaded.append(list(srcs))
            return [{"dataUrl": f"data:image/jpeg;base64,{s[-5:]}"} if not s.endswith("2.jpg") else {"error": "404"} for s in srcs]
    fake.ctx.vision = Vision()
    fake.ctx.analyze = FakeLook()
    out = run_tool(view_images_tool, {"task": "描述"}, fake.ctx)
    assert out["ok"] and loaded == [["https://img.example.com/1.jpg", "https://img.example.com/2.jpg", "https://img.example.com/3.jpg"]]
    assert fake.ctx.analyze.calls[0][1] == ["image 1 (¶2)", "image 3 (¶6)"] and "image 2: 404" in out["content"]
    fake.ctx.analyze = None
    assert not run_tool(view_images_tool, {"task": "描述"}, fake.ctx)["ok"]


def test_look_in_batches_sends_eight_pictures_a_call_and_keeps_partial_lines_on_failure():
    from wc_agent.polish import look_in_batches
    sent: List[Any] = []

    async def transport(system, user, images=None):
        sent.append(images)
        if len(sent) == 3:
            raise RuntimeError("429")
        return "\n".join(f"image {i}: x" for i in range(len(images)))
    items = [{"label": f"image {n}", "dataUrl": f"d{n}"} for n in range(1, 21)]
    out = asyncio.run(look_in_batches("t", items, transport))
    assert [len(b) for b in sent] == [8, 8, 4] and len(out["lines"]) == 16
    assert out["failed"] == "429" and out["batches"] == 2 and out["total"] == 3


def test_a_page_written_as_text_and_brs_in_a_div_is_read_in_order():
    pytest.importorskip("bs4")
    html = ('<body><div class="links">首页</div><div class="body-text">雨下了一整夜，<b>小镇</b>很安静。<br/>清晨的渡口没有人。<br>'
            '<img src="https://img.example.com/a.jpg" alt="渡口"><br><font color="red">后来</font>，船终于来了。<!-- 注释 --></div>'
            '<h2>后记</h2><p>全文完<img src="https://img.example.com/b.jpg"></p></body>')
    _, paragraphs, images = server_web.readable_paragraphs(html, "https://blog.example.com/p.html")
    assert paragraphs == ["首页", "雨下了一整夜，小镇很安静。", "清晨的渡口没有人。", "[image 1: 渡口]", "后来，船终于来了。", "# 后记", "全文完", "[image 2]"]
    assert [i["url"] for i in images] == ["https://img.example.com/a.jpg", "https://img.example.com/b.jpg"]
