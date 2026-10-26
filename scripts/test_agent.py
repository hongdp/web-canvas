"""The server-side agent loop and tools against an in-memory book
(wc_agent; a port of the essentials of src/agent/__tests__)."""
import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from wc_agent.registry import Tool, ToolRegistry
from wc_agent.run import AgentRun, StepOutput
from wc_agent.tools.book_reads import BOOK_TOOLS, read_chapter_tool
from wc_agent.tools.document_writes import DOCUMENT_WRITE_TOOLS, similar_chapters, user_edited
from wc_agent.types import ToolContext, create_run_state
from wc_text.diff import strip_diff_markup
from wc_text.policy import DEFAULT_BUDGETS, DEFAULT_POLICY
from wc_text.stream_handlers import NO_ACTION_RETRY_INSTRUCTION


class FakeBook:
    """Every port over one in-memory book: no editor, no store, every call recorded."""

    def __init__(self, original: str, chapters: Optional[List[Dict[str, Any]]] = None, selected_text: str = "",
                 in_context: Optional[List[str]] = None, lazy: Optional[Dict[str, str]] = None) -> None:
        self.book: List[Dict[str, Any]] = [{"id": "doc-1", "title": "Chapter 1", "content": original}, *(chapters or [])]
        self.lazy = dict(lazy or {})
        self.writes: List[Dict[str, str]] = []
        self.snapshots: List[str] = []
        self.opened: List[str] = []
        self.removed: List[str] = []
        self.progress: List[Optional[str]] = []
        self.previews: List[str] = []
        self.selection_previews: List[str] = []
        self.open_id = "doc-1"
        self.moved = False
        self.created = 0
        self.selected_text = selected_text
        book = self

        class Document:
            start_id = "doc-1"
            original = ""

            def chapters(self):
                return [{**c, "loaded": c["id"] not in book.lazy} for c in book.book]

            def open_id(self):
                return book.open_id

            def user_moved(self):
                return book.moved

            async def ensure_loaded(self, ids):
                for i in ids:
                    if i in book.lazy:
                        ch = next((c for c in book.book if c["id"] == i), None)
                        if ch:
                            ch["content"] = book.lazy.pop(i)

            def commit(self, doc_id, html):
                book.writes.append({"id": doc_id, "html": html})
                ch = next((c for c in book.book if c["id"] == doc_id), None)
                if ch:
                    ch["content"] = html

            def open(self, doc_id):
                book.open_id = doc_id
                book.opened.append(doc_id)

            def create(self, title):
                book.created += 1
                doc_id = f"new-{book.created}"
                book.book.append({"id": doc_id, "title": title, "content": "<p></p>"})
                return doc_id

            def move(self, doc_id, index):
                ch = next((c for c in book.book if c["id"] == doc_id), None)
                if ch:
                    book.book.remove(ch)
                    book.book.insert(max(0, min(index, len(book.book))), ch)

            def rename(self, doc_id, title):
                ch = next((c for c in book.book if c["id"] == doc_id), None)
                if ch:
                    ch["title"] = title

            def remove(self, doc_id):
                book.removed.append(doc_id)
                book.book = [c for c in book.book if c["id"] != doc_id]
                if book.open_id == doc_id:
                    book.open_id = book.book[0]["id"] if book.book else ""

            def snapshot(self, doc_id, label):
                book.snapshots.append(doc_id)

        class Editor:
            def preview_document(self, html):
                book.previews.append(html)

            def preview_selection(self, html):
                book.selection_previews.append(html)

            def discard_preview(self):
                pass

        class Selection:
            def range(self):
                return True if book.selected_text else None

            def original_text(self):
                return book.selected_text

        class Images:
            def __init__(self):
                self.registered = []

            def preserve(self, h):
                return h

            def restore(self, h):
                return h

            def register(self, data_url, alt=""):
                self.registered.append({"dataUrl": data_url, "alt": alt})
                return f"{{{{IMAGE_PLACEHOLDER_{len(self.registered) - 1}}}}}"

        class Ui:
            def progress(self, line):
                book.progress.append(line)

            def writing(self, doc_id):
                pass

        document = Document()
        document.original = original
        self.ctx = ToolContext(document, Editor(), Selection(), Images(), Ui(),
                               create_run_state("doc-1", in_context, original))

    def user_edits(self, doc_id: str, html: str) -> None:
        ch = next(c for c in self.book if c["id"] == doc_id)
        ch["content"] = html

    def last_write(self, doc_id: str) -> Optional[str]:
        return next((w["html"] for w in reversed(self.writes) if w["id"] == doc_id), None)


def run_tool(tool: Tool, args: Dict[str, Any], ctx: ToolContext, call: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    inv = {"id": "x", "name": tool.name, "args": args, "source": "native", **(call or {})}
    return asyncio.run(tool.invoke(inv, ctx))


# ── writes ────────────────────────────────────────────────────────────────────

def test_second_edit_matches_text_the_first_wrote():
    fake = FakeBook("<p>alpha</p>")
    update, edit, _ = DOCUMENT_WRITE_TOOLS
    assert run_tool(edit, {"edits": [{"search": "<p>alpha</p>", "replace": "<p>beta</p>"}]}, fake.ctx)["ok"]
    out = run_tool(edit, {"edits": [{"search": "<p>beta</p>", "replace": "<p>gamma</p>"}]}, fake.ctx)
    assert out["ok"], out
    assert strip_diff_markup(fake.last_write("doc-1")) == "<p>gamma</p>"
    assert "diff-deletion" in fake.last_write("doc-1")


def test_a_chapter_with_only_a_picture_paragraph_is_rewritten_from_its_text_read():
    pictures = '<p>一。</p><p>二。</p><p><img src="data:image/png;base64,AAA" width="600" style=""> <img src="data:image/png;base64,BBB"></p>'
    fake = FakeBook("<p>start</p>", chapters=[{"id": "doc-9", "title": "有图", "content": pictures}])
    read = run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)
    assert "a rewrite may leave them out" in read["content"] and 'read it with format="html" first' not in read["content"]
    out = run_tool(DOCUMENT_WRITE_TOOLS[0], {"chapter": "2", "html": "<p>一，改。</p><p>二，改。</p><p>[image] [image]</p>"}, fake.ctx)
    assert out["ok"], out
    written = strip_diff_markup(fake.last_write("doc-9"))
    assert "一，改。" in written and "[image]" not in written and "base64,AAA" in written and "base64,BBB" in written


def test_replace_text_across_chapters_in_text_only():
    from wc_agent.tools.document_writes import replace_text_tool
    fake = FakeBook("<p>阿禾来了。</p>", chapters=[{"id": "doc-2", "title": "二", "content": '<p>阿禾和王小明。王小明笑了。</p><p><img alt="阿禾和王小明" src="x.png"></p>'}])
    expected = run_tool(replace_text_tool, {"find": "王", "replace": "李", "chapters": ["2"], "expect": 1}, fake.ctx)
    assert expected["ok"] is False and "there are 2" in expected["content"] and fake.writes == []
    out = run_tool(replace_text_tool, {"find": "阿禾", "replace": "阿棠", "chapters": ["all"]}, fake.ctx)
    assert out["ok"] and out["trace"] == "🔁 replaced 阿禾→阿棠: 2 in 2 chapters"
    assert strip_diff_markup(fake.last_write("doc-1")) == "<p>阿棠来了。</p>"
    assert 'alt="阿禾和王小明"' in fake.last_write("doc-2") and "阿棠和王小明" in strip_diff_markup(fake.last_write("doc-2"))
    regex = run_tool(replace_text_tool, {"find": "王(小.)", "replace": "李$1", "chapters": ["2"], "regex": True, "preview": True}, fake.ctx)
    assert regex["ok"] and "[王小明→李小明]" in regex["content"]


def test_the_running_count_shows_for_the_open_chapter_too():
    from wc_agent.tools.document_writes import preview_rewrite, preview_selection_with_count
    fake = FakeBook("<p>old</p>")
    preview_rewrite(fake.ctx, None, "<p>春江潮水连海平</p>")
    assert fake.progress[-1] == '✍️ #1 "Chapter 1" … 7 chars' and fake.previews
    preview_selection_with_count(fake.ctx, "<p>重写后的文字</p>")
    assert fake.progress[-1] == "✍️ the selection … 6 chars"


def test_rewrite_guards_truncated_and_empty():
    fake = FakeBook("<p>alpha</p>")
    update = DOCUMENT_WRITE_TOOLS[0]
    out = run_tool(update, {"html": "<p>cut"}, fake.ctx, {"unclosed": True})
    assert out["ok"] is False and out["effects"]["canvasIssue"] == "truncated"
    assert fake.last_write("doc-1") == "<p>alpha</p>"
    out = run_tool(update, {"html": "   "}, fake.ctx)
    assert out["effects"] == {"producedNothing": True}


def test_edit_on_unread_chapter_is_refused_then_allowed_after_html_read():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "Chapter 2", "content": "<p>two</p>"}])
    edit = DOCUMENT_WRITE_TOOLS[1]
    out = run_tool(edit, {"chapter": "2", "edits": [{"search": "<p>two</p>", "replace": "<p>TWO</p>"}]}, fake.ctx)
    assert out["ok"] is False and out["retryable"] is True and "not read yet" in out["trace"]
    assert run_tool(read_chapter_tool, {"chapters": ["2"], "format": "html"}, fake.ctx)["ok"]
    out = run_tool(edit, {"chapter": "2", "edits": [{"search": "<p>two</p>", "replace": "<p>TWO</p>"}]}, fake.ctx)
    assert out["ok"], out
    assert fake.snapshots == ["doc-2"]
    assert "TWO" in fake.last_write("doc-2")


def test_user_edit_meanwhile_refuses_the_write_and_keeps_their_text():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "Chapter 2", "content": "<p>two</p>"}])
    edit = DOCUMENT_WRITE_TOOLS[1]
    run_tool(read_chapter_tool, {"chapters": ["2"], "format": "html"}, fake.ctx)
    fake.user_edits("doc-2", "<p>two, by the user</p>")
    out = run_tool(edit, {"chapter": "2", "edits": [{"search": "<p>two</p>", "replace": "<p>TWO</p>"}]}, fake.ctx)
    assert out["ok"] is False and "edited by the user meanwhile" in out["trace"]
    assert fake.last_write("doc-2") is None


def test_accepting_the_review_diff_meanwhile_is_not_a_user_edit():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "Chapter 2", "content": "<p>one</p><p>two</p>"}])
    edit = DOCUMENT_WRITE_TOOLS[1]
    run_tool(read_chapter_tool, {"chapters": ["2"], "format": "html"}, fake.ctx)
    assert run_tool(edit, {"chapter": "2", "edits": [{"search": "<p>one</p>", "replace": "<p>ONE</p>"}]}, fake.ctx)["ok"]
    fake.user_edits("doc-2", "<p>ONE</p>\n<p>two</p>")
    out = run_tool(edit, {"chapter": "2", "edits": [{"search": "<p>two</p>", "replace": "<p>TWO</p>"}]}, fake.ctx)
    assert out["ok"], out
    written = fake.last_write("doc-2")
    assert strip_diff_markup(written) == "<p>ONE</p><p>TWO</p>"
    assert "one" not in written


def test_an_accepted_copy_re_spelled_by_the_editor_is_not_a_user_edit():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "Chapter 2", "content": "<p>one</p><ul><li>two</li></ul>"}])
    edit = DOCUMENT_WRITE_TOOLS[1]
    run_tool(read_chapter_tool, {"chapters": ["2"], "format": "html"}, fake.ctx)
    assert run_tool(edit, {"chapter": "2", "edits": [{"search": "<p>one</p>", "replace": "<p>ONE</p>"}]}, fake.ctx)["ok"]
    fake.user_edits("doc-2", "<p>ONE</p><ul><li>two</li></ul>")
    assert user_edited(fake.ctx, "doc-2") is False
    fake.user_edits("doc-2", "<p>ONE</p><ul><li><p>two</p></li></ul><p></p>")
    assert user_edited(fake.ctx, "doc-2") is False
    fake.user_edits("doc-2", "<p><strong>ONE</strong></p><ul><li><p>two</p></li></ul><p></p>")
    assert user_edited(fake.ctx, "doc-2") is True


def test_rejecting_the_review_diff_meanwhile_is_still_a_user_edit():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "Chapter 2", "content": "<p>one</p><p>two</p>"}])
    edit = DOCUMENT_WRITE_TOOLS[1]
    run_tool(read_chapter_tool, {"chapters": ["2"], "format": "html"}, fake.ctx)
    run_tool(edit, {"chapter": "2", "edits": [{"search": "<p>one</p>", "replace": "<p>ONE</p>"}]}, fake.ctx)
    fake.user_edits("doc-2", "<p>one</p><p>two</p>")
    out = run_tool(edit, {"chapter": "2", "edits": [{"search": "<p>two</p>", "replace": "<p>TWO</p>"}]}, fake.ctx)
    assert out["ok"] is False and "edited by the user meanwhile" in out["trace"]


def test_new_chapter_is_created_by_the_write_and_rewrite_answers_differently():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "第二章 进城", "content": "<p>two</p>"}])
    update = DOCUMENT_WRITE_TOOLS[0]
    out = run_tool(update, {"new_chapter": "第二章 入城", "html": "<p>new text</p>"}, fake.ctx)
    assert out["ok"] and "Created a NEW chapter" in out["content"] and "rename_chapter" in out["content"]
    assert fake.opened == ["new-1"] and strip_diff_markup(fake.last_write("new-1")) == "<p>new text</p>"
    out = run_tool(update, {"html": "<p>rewritten</p>"}, fake.ctx)
    assert "Rewrote the EXISTING chapter" in out["content"] and "No chapter was added" in out["content"]


def test_a_rewrite_for_a_missing_chapter_is_kept_and_placed_by_an_empty_new_chapter_write():
    """backend_authority.md §6.2: the text is not lost to a wrong number; an empty-html write confirms where it goes."""
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "第二章", "content": "<p>two</p>"}])
    update = DOCUMENT_WRITE_TOOLS[0]
    out = run_tool(update, {"chapter": "7", "html": "<h1>第七章</h1><p>很长的正文。</p>"}, fake.ctx)
    assert out["ok"] is False and "There is no chapter #7" in out["content"] and "KEPT" in out["content"]
    assert out["trace"].startswith("📥 kept")
    assert fake.ctx.run.held_text["ref"] == "7"
    out = run_tool(update, {"new_chapter": "第七章", "html": ""}, fake.ctx)
    assert out["ok"], out
    assert strip_diff_markup(fake.last_write("new-1")) == "<h1>第七章</h1><p>很长的正文。</p>"
    assert fake.ctx.run.held_text is None
    # Nothing kept any more: an empty html is the old refusal.
    out = run_tool(update, {"new_chapter": "第八章", "html": ""}, fake.ctx)
    assert out["ok"] is False and "empty html" in out["content"]


def test_kept_text_can_go_to_an_existing_chapter_under_the_usual_rules():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "第二章", "content": "<p>two</p>"}])
    update = DOCUMENT_WRITE_TOOLS[0]
    run_tool(update, {"chapter": "第九章", "html": "<p>kept</p>"}, fake.ctx)
    assert fake.ctx.run.held_text is not None
    out = run_tool(update, {"chapter": "2", "html": ""}, fake.ctx)
    assert out["ok"] is False and "not rewritten" in out["content"]   # not read: refused, and still kept
    assert fake.ctx.run.held_text is not None
    run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)
    assert run_tool(update, {"chapter": "2", "html": ""}, fake.ctx)["ok"]
    assert strip_diff_markup(fake.last_write("doc-2")) == "<p>kept</p>"


def test_a_cut_off_rewrite_for_a_missing_chapter_is_not_kept():
    fake = FakeBook("<p>alpha</p>")
    out = run_tool(DOCUMENT_WRITE_TOOLS[0], {"chapter": "5", "html": "<p>half"}, fake.ctx, {"unclosed": True})
    assert "KEPT" not in out["content"] and fake.ctx.run.held_text is None


def test_new_chapter_title_of_an_unread_chapter_is_refused():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "Chapter 2", "content": "<p>two</p>"}])
    out = run_tool(DOCUMENT_WRITE_TOOLS[0], {"new_chapter": "Chapter 2", "html": "<p>x</p>"}, fake.ctx)
    assert out["ok"] is False and "not overwritten" in out["trace"]
    assert fake.last_write("doc-2") is None and fake.created == 0


def test_a_selection_rewrite_inside_a_paragraph_drops_the_tags_line_breaks():
    # <selection_replace>\n…\n</selection_replace>: each break used to become a space.
    fake = FakeBook("<p>她到了。门轴吱呀作响。茶还是温的。</p>", selected_text="门轴吱呀作响。")
    replace = DOCUMENT_WRITE_TOOLS[2]
    out = run_tool(replace, {"html": "\n门轴缓缓合上了。\n"}, fake.ctx)
    assert out["ok"], out
    assert strip_diff_markup(fake.last_write("doc-1")) == "<p>她到了。门轴缓缓合上了。茶还是温的。</p>"


def test_trim_tag_layout_keeps_a_plain_space():
    from wc_agent.tools.document_writes import trim_tag_layout
    assert trim_tag_layout("\n  text\n") == "text"
    assert trim_tag_layout(" text ") == " text "
    assert trim_tag_layout("\n<p>a</p>\n<p>b</p>\n") == "<p>a</p>\n<p>b</p>"


def test_selection_rewrite_is_placed_by_text_and_edits_beside_it_apply_locally():
    fake = FakeBook("<p>Keep this. Change this.</p><p>other</p>", selected_text="Change this.")
    _, edit, replace = DOCUMENT_WRITE_TOOLS
    out = run_tool(replace, {"html": "<p>Changed.</p>"}, fake.ctx)
    assert out["ok"], out
    html = fake.last_write("doc-1")
    assert strip_diff_markup(html) == "<p>Keep this. Changed.</p><p>other</p>"
    out = run_tool(edit, {"edits": [{"search": "<p>other</p>", "replace": "<p>others</p>"}]}, fake.ctx)
    assert out["ok"], out
    assert "others" in fake.last_write("doc-1")


def test_rename_and_replace():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "第二章 进城", "content": "<h1>第二章 进城</h1><p>old</p>"}])
    update, rename = DOCUMENT_WRITE_TOOLS[0], BOOK_TOOLS[-1]
    run_tool(update, {"new_chapter": "第二章 入城", "html": "<h1>第二章 入城</h1><p>new</p>"}, fake.ctx)
    out = run_tool(rename, {"chapter": "3", "title": "第二章 进城"}, fake.ctx)
    assert out["ok"] is False and "replace=true" in out["content"]
    out = run_tool(rename, {"chapter": "3", "title": "第二章 进城", "replace": True}, fake.ctx)
    assert out["ok"], out
    assert fake.removed == ["new-1"]
    assert strip_diff_markup(fake.last_write("doc-2")) == "<h1>第二章 进城</h1><p>new</p>"


def test_similar_chapters_never_pairs_different_numbers():
    chapters = [{"id": "a", "title": "第二章 进城"}, {"id": "b", "title": "第三章 入城"}]
    assert [c["number"] for c in similar_chapters("第二章 入城", "x", chapters)] == [1]


# ── reads ─────────────────────────────────────────────────────────────────────

def test_read_chapter_numbers_paragraphs_and_pages():
    paras = "".join(f"<p>{'段落' * 300}{i}</p>" for i in range(40))
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "Long", "content": paras}])
    out = run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)
    assert out["ok"] and "¶1 " in out["content"] and "Stopped at ¶" in out["content"]
    out = run_tool(read_chapter_tool, {"chapters": ["2"], "paragraphs": "\"38-\""}, fake.ctx)
    assert "¶38 " in out["content"] and "¶37 " not in out["content"]
    out = run_tool(read_chapter_tool, {"chapters": ["2"], "paragraphs": "-2"}, fake.ctx)
    assert "¶2 " in out["content"] and "¶3 " not in out["content"]


def test_read_repeat_guard_and_in_context():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "Two", "content": "<p>two</p>"}], in_context=["doc-2"])
    out = run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)
    assert "already in your context" in out["content"]
    fake.ctx.run.step = 3
    assert "¶1 two" in run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)["content"]
    assert "already returned in step 4" in run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)["content"]


def test_grep_finds_across_chapters_with_paragraph_numbers():
    fake = FakeBook("<p>阿青来了</p>", chapters=[{"id": "doc-2", "title": "阿红", "content": "<p>x</p><p>阿青又来了</p>"}])
    out = run_tool(BOOK_TOOLS[1], {"pattern": "阿青|阿红"}, fake.ctx)
    assert '#1 "Chapter 1" ¶1' in out["content"] and '#2 "阿红" ¶2' in out["content"]
    assert "2 match(es) in 2 chapter(s)" in out["content"]
    out = run_tool(BOOK_TOOLS[1], {"pattern": "("}, fake.ctx)
    assert "plain text" in out["content"]


def test_grep_and_read_show_an_image_inside_a_text_paragraph():
    fake = FakeBook("<p>start</p>", chapters=[{"id": "doc-2", "title": "附图页", "content": "<p>山间起了雾。<img src=\"pic\"></p><p>后面。</p>"}])
    out = run_tool(BOOK_TOOLS[1], {"pattern": "image", "chapters": ["2"]}, fake.ctx)
    assert '#2 "附图页" ¶1: 山间起了雾。 [image]' in out["content"]
    assert "¶1 山间起了雾。 [image]" in run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)["content"]


def test_list_chapters_says_when_nothing_changed():
    fake = FakeBook("<p>alpha</p>")
    from wc_agent.types import chapter_outline
    fake.ctx.run.start_outline = chapter_outline(fake.ctx.document.chapters())
    out = run_tool(BOOK_TOOLS[2], {}, fake.ctx)
    assert "No chapter has been added" in out["content"]
    assert "identical to your previous" in run_tool(BOOK_TOOLS[2], {}, fake.ctx)["content"]


def test_delete_removes_an_empty_chapter_and_asks_about_one_with_text():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "Two", "content": "<p>two</p>"}, {"id": "doc-3", "title": "Empty", "content": "<p></p>"}])
    delete = BOOK_TOOLS[4]
    asked = run_tool(delete, {"chapter": "2"}, fake.ctx)
    assert asked["ok"] and "Delete / Keep" in asked["content"] and fake.removed == []
    assert fake.ctx.run.question["confirm"] == {"action": "delete_chapter", "documentId": "doc-2", "title": "Two", "number": 2,
                                                "chars": 3, "contentHash": hash_content("<p>two</p>")}
    out = run_tool(delete, {"chapter": "3"}, fake.ctx)
    assert out["ok"] and fake.removed == ["doc-3"]
    # One question at a time.
    fake.book.append({"id": "doc-4", "title": "Four", "content": "<p>four</p>"})
    again = run_tool(delete, {"chapter": "Four"}, fake.ctx)
    assert again["ok"] is False and "one question is asked at a time" in again["content"]


DELETE_TWO = [{"id": "doc-2", "title": "第7章　渡口", "content": "<h1>第7章　渡口</h1><p>seven</p>"},
              {"id": "doc-3", "title": "第8章　雨夜", "content": "<h1>第8章　雨夜</h1><p>eight</p>"}]


def test_the_start_chapter_is_asked_about_and_deleted_at_the_answer():
    # "Merge #12 into #11", asked with #12 open: the start chapter used to be refused outright.
    h = Harness([calls("", ("d", "delete_chapter", '{"chapter":"1"}')), DONE], extra=[BOOK_TOOLS[4]], chapters=[dict(DELETE_TWO[1])])
    asyncio.run(h.run.start())
    assert h.run.paused["confirm"]["documentId"] == "doc-1" and h.fake.removed == []
    asyncio.run(h.run.resume_with_answer("删除", confirmed=True))
    assert h.fake.removed == ["doc-1"] and '#1 "Chapter 1" was deleted, as the user confirmed.' in h.requests[1][-1]["content"]


def test_a_selection_turns_chapter_is_not_deleted():
    fake = FakeBook("<p>alpha beta</p>", chapters=[dict(DELETE_TWO[1])], selected_text="beta")
    out = run_tool(BOOK_TOOLS[4], {"chapter": "1"}, fake.ctx)
    assert out["ok"] is False and "holds the selection" in out["content"] and fake.ctx.run.question is None


def test_a_range_written_as_an_attribute_is_read():
    from wc_agent.tools.book_reads import _parse_range
    assert _parse_range('="60-"') == {"from": 60, "to": None}
    assert _parse_range('= "40-60"') == {"from": 40, "to": 60}


def test_a_write_after_a_truncated_rewrite_clears_the_cut_off_warning():
    # Step 1: a whole rewrite cut off at max_tokens; step 2: the same change as edits. The turn is not "cut off".
    cut = StepOutput("<canvas><p>cut off here", [])
    h = Harness([cut, text("<edit><<<<<<< SEARCH\n<p>alpha</p>\n=======\n<p>beta</p>\n>>>>>>> REPLACE\n</edit>\n<doc_status>updated</doc_status>"), DONE],
                policy={"continueAfterWrites": True})
    asyncio.run(h.run.start())
    assert h.summary["effects"]["canvasIssue"] is None and "beta" in h.fake.last_write("doc-1")
    only_cut = Harness([cut, DONE], policy={"continueAfterWrites": True})
    asyncio.run(only_cut.run.start())
    assert only_cut.summary["effects"]["canvasIssue"] == "truncated"


def test_only_the_delete_button_deletes_a_chapter_with_text():
    def harness():
        h = Harness([calls("", ("d", "delete_chapter", '{"chapter":"2"}')), DONE], extra=[BOOK_TOOLS[4]], chapters=[dict(c) for c in DELETE_TWO])
        asyncio.run(h.run.start())
        assert h.summary is None and h.run.paused["confirm"]["documentId"] == "doc-2" and h.fake.removed == []
        return h

    pressed = harness()
    asyncio.run(pressed.run.resume_with_answer("删除 #2「第7章　渡口」。", confirmed=True))
    assert pressed.fake.removed == ["doc-2"]
    told = pressed.requests[1][-1]["content"]
    assert told.startswith("The user answered: 删除") and '#2 "第7章　渡口" was deleted, as the user confirmed.' in told and "#3 is now #2" in told

    # A typed "yes" is not the button.
    typed = harness()
    asyncio.run(typed.run.resume_with_answer("yes, delete it"))
    assert typed.fake.removed == [] and '#2 "第7章　渡口" was not deleted.' in typed.requests[1][-1]["content"]

    # Changed after the question: kept, and the model is told why.
    changed = harness()
    changed.fake.book[1]["content"] = "<h1>第7章　渡口</h1><p>seven, revised</p>"
    asyncio.run(changed.run.resume_with_answer("删除", confirmed=True))
    assert changed.fake.removed == [] and "was NOT deleted: it changed after the question was asked" in changed.requests[1][-1]["content"]


def test_a_deletion_runs_after_the_renames_of_its_reply():
    # The model numbers every call from the index it sees: delete #2 (empty, so at once), rename #3.
    # Deleting first would make #3 the end of a two-chapter book: "There is no chapter #3".
    reply = calls("", ("d", "delete_chapter", '{"chapter":"2"}'), ("r", "rename_chapter", '{"chapter":"3","title":"第7章　雨夜"}'))
    chapters = [{"id": "doc-2", "title": "第7章　渡口", "content": "<p></p>"}, dict(DELETE_TWO[1])]
    h = Harness([reply, DONE], extra=[BOOK_TOOLS[4], BOOK_TOOLS[-1]], chapters=chapters)
    asyncio.run(h.run.start())
    eight = next(c for c in h.fake.book if c["id"] == "doc-3")
    assert h.fake.removed == ["doc-2"]
    assert eight["title"] == "第7章　雨夜" and eight["content"] == "<h1>第7章　雨夜</h1><p>eight</p>"


def test_rename_carries_a_heading_that_read_the_old_title():
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "第8章　雨夜", "content": "<h1>第8章　雨夜</h1><p>x</p>"},
                                            {"id": "doc-3", "title": "第9章", "content": "<h1>序幕</h1><p>y</p>"}])
    rename = BOOK_TOOLS[-1]
    out = run_tool(rename, {"chapter": "2", "title": "第7章　雨夜"}, fake.ctx)
    assert out["ok"] and "now reads the new one" in out["content"]
    assert fake.book[1]["content"] == "<h1>第7章　雨夜</h1><p>x</p>" and fake.book[1]["title"] == "第7章　雨夜"
    kept = run_tool(rename, {"chapter": "3", "title": "第8章"}, fake.ctx)
    assert 'still reads "序幕"' in kept["content"] and fake.book[2]["content"] == "<h1>序幕</h1><p>y</p>"


# ── the run ───────────────────────────────────────────────────────────────────

def read_tool(execute=None):
    async def default(args, ctx, call):
        return {"ok": True, "content": f"TEXT OF {args.get('chapter')}", "trace": f"read {args.get('chapter')}"}
    return Tool(name="read_chapter", description="Read.", parameters={"type": "object", "properties": {"chapter": {"type": "string"}}},
                kind="read", execute=execute or default)


class Harness:
    def __init__(self, replies, *, write_protocol="markup", budgets=None, policy=None, can_continue=True, original="<p>alpha</p>",
                 extra=None, guard=None, chapters=None, carried_plan=None):
        self.fake = FakeBook(original, chapters=chapters)
        self.registry = ToolRegistry([*DOCUMENT_WRITE_TOOLS, read_tool(), *(extra or [])])
        self.requests: List[List[Dict[str, Any]]] = []
        self.corrective: List[Any] = []
        self.paused: List[Any] = []
        self.summary: Optional[Dict[str, Any]] = None
        self.replies = list(replies)
        harness = self

        async def driver(messages, step, final):
            harness.requests.append(messages)
            if not harness.replies:
                raise AssertionError("scripted replies exhausted")
            nxt = harness.replies.pop(0)
            return nxt(messages) if callable(nxt) else nxt

        class Observer:
            def on_corrective(self, failure, attempt, mx):
                harness.corrective.append((failure, attempt, mx))

            def on_step_executed(self, progress):
                pass

            def on_paused(self, reason, progress):
                harness.paused.append(reason)

            def on_asked(self, question, progress):
                harness.paused.append({"reason": "question", **question})

            def on_finish(self, summary):
                harness.summary = summary

        self.run = AgentRun(registry=self.registry, ctx=self.fake.ctx, write_protocol=write_protocol, driver=driver, observer=Observer(),
                            budgets={**DEFAULT_BUDGETS, **(budgets or {})}, policy={**DEFAULT_POLICY, **(policy or {})},
                            can_continue=can_continue, initial_messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "request"}],
                            guard=guard, carried_plan=carried_plan)


def text(t: str) -> StepOutput:
    return StepOutput(t)


DONE = text("done\n<doc_status>unchanged</doc_status>")


def calls(t: str, *c) -> StepOutput:
    return StepOutput(t, [{"id": i, "name": n, "args": json.loads(a), "argumentsText": a} for i, n, a in c])


def test_markup_turn_applies_a_rewrite_and_ends():
    h = Harness([text("Sure.\n<canvas><p>beta</p></canvas>\n<doc_status>updated</doc_status>")])
    asyncio.run(h.run.start())
    assert h.summary["endReason"] == "writes_done" and h.summary["steps"] == 1
    assert strip_diff_markup(h.fake.last_write("doc-1")) == "<p>beta</p>"


def test_writes_continue_the_turn_and_a_plain_reply_ends_it():
    h = Harness([text("<canvas><p>beta</p></canvas>"), text("Done.")], policy={"continueAfterWrites": True, "feedBackFailedWrites": True})
    asyncio.run(h.run.start())
    assert h.summary["endReason"] == "answered" and h.summary["steps"] == 2
    assert h.requests[1][-1]["content"].startswith("RESULT OF YOUR DOCUMENT CHANGES")
    assert h.summary["chatText"] == "Done."


def test_protocol_failure_is_retried_by_appending():
    h = Harness([text("I changed it."), text("<canvas><p>beta</p></canvas>\n<doc_status>updated</doc_status>")])
    asyncio.run(h.run.start())
    assert h.corrective == [("undeclared", 1, 3)]
    assert h.requests[1][:2] == h.requests[0] and h.requests[1][-1]["content"] == NO_ACTION_RETRY_INSTRUCTION
    assert h.summary["endReason"] == "writes_done"


def _miss(i):
    return calls("", (i, "edit_document", json.dumps({"edits": [{"search": f"<p>nowhere {i}</p>", "replace": "<p>x</p>"}]})))


def test_failed_writes_are_counted_in_a_row():
    hit = calls("", ("ok", "edit_document", json.dumps({"edits": [{"search": "<p>alpha</p>", "replace": "<p>beta</p>"}]})))
    h = Harness([_miss("m1"), _miss("m2"), hit, _miss("m3"), _miss("m4"), _miss("m5"), text("好了。")], write_protocol="tools",
                budgets={"maxSteps": 0}, policy={"continueAfterWrites": True, "feedBackFailedWrites": True})
    asyncio.run(h.run.start())
    assert len(h.requests) == 7 and h.summary["endReason"] == "answered"


def test_a_turn_stuck_on_failed_writes_still_ends():
    h = Harness([_miss("m1"), _miss("m2"), _miss("m3"), _miss("m4"), text("unused")], write_protocol="tools",
                policy={"continueAfterWrites": True, "feedBackFailedWrites": True})
    asyncio.run(h.run.start())
    assert len(h.requests) == 4 and h.summary["endReason"] == "corrective_exhausted"


def test_read_is_answered_under_its_call_id_with_exact_bytes():
    h = Harness([calls("Reading.", ("c1", "read_chapter", '{"chapter":  "2"}')), text("Answer.\n<doc_status>unchanged</doc_status>")])
    asyncio.run(h.run.start())
    assistant, tool = h.requests[1][2], h.requests[1][3]
    assert assistant["toolCalls"][0]["argumentsText"] == '{"chapter":  "2"}'
    assert tool == {"role": "tool", "toolCallId": "c1", "name": "read_chapter", "content": "TEXT OF 2"}
    assert h.summary["endReason"] == "answered" and h.summary["trace"] == ["read 2"]


def test_step_limit_sends_the_final_step_with_the_note():
    h = Harness([calls("r", ("c1", "read_chapter", '{"chapter":"1"}')), calls("r", ("c2", "read_chapter", '{"chapter":"2"}')), text("done")], budgets={"maxSteps": 2})
    asyncio.run(h.run.start())
    assert h.summary["endReason"] == "step_limit" and h.summary["steps"] == 2
    assert "step budget" in h.requests[1][-1]["content"]


def test_unlimited_steps_when_the_limit_is_zero():
    h = Harness([calls("r", ("c%d" % i, "read_chapter", '{"chapter":"%d"}' % i)) for i in range(8)] + [DONE], budgets={"maxSteps": 0})
    asyncio.run(h.run.start())
    assert h.summary["steps"] == 9 and h.summary["endReason"] == "answered"


def test_guard_pauses_between_steps_and_resume_continues():
    pauses = {"count": 0}

    def guard(run):
        if run.steps_taken == 1 and pauses["count"] == 0:
            pauses["count"] += 1
            return {"reason": "repeating"}
        return None
    h = Harness([calls("r", ("c1", "read_chapter", '{"chapter":"1"}')), DONE], guard=guard)
    asyncio.run(h.run.start())
    assert h.paused == [{"reason": "repeating"}] and h.summary is None and h.run.paused
    asyncio.run(h.run.resume())
    assert h.summary["endReason"] == "answered" and h.summary["steps"] == 2


def test_snapshot_and_restore_continue_from_the_last_completed_step():
    h = Harness([calls("r", ("c1", "read_chapter", '{"chapter":"1"}'))], guard=lambda run: {"reason": "x"} if run.steps_taken == 1 else None)
    asyncio.run(h.run.start())
    snap = json.loads(json.dumps(h.run.snapshot()))
    h2 = Harness([DONE])
    h2.run.restore(snap, lambda doc_id: next((c["content"] for c in h2.fake.book if c["id"] == doc_id), None))
    asyncio.run(h2.run.resume())
    assert h2.summary["steps"] == 2 and h2.summary["trace"] == ["read 1"]
    assert h2.requests[0][2]["toolCalls"][0]["id"] == "c1"


def test_cancel_ends_the_run_without_dropping_created_chapters():
    def stop_after_first(messages):
        h.run.cancel()
        return text("<canvas new_chapter=\"New\"><p>draft")
    h = Harness([stop_after_first])
    asyncio.run(h.run.start())
    assert h.summary["endReason"] == "cancelled"
    assert any(c["title"] == "New" for c in h.fake.book)


def test_empty_created_chapter_is_dropped_at_the_end():
    h = Harness([text("<canvas new_chapter=\"New\"><p>cut"), DONE], policy={"continueAfterWrites": True, "feedBackFailedWrites": True})
    asyncio.run(h.run.start())
    assert not any(c["title"] == "New" for c in h.fake.book) and h.fake.removed == ["new-1"]


def test_markup_model_gets_update_document_natively_beside_its_tag():
    h = Harness([text("x")])
    names = [t.name for t in h.run.offered_tools()]
    assert names == ["update_document", "read_chapter"]
    h2 = Harness([text("x")], write_protocol="tools")
    assert [t.name for t in h2.run.offered_tools()] == ["update_document", "edit_document", "read_chapter"]


def test_tags_beside_a_native_write_are_both_applied():
    h = Harness([StepOutput('<canvas chapter="2"><p>tagged</p></canvas>', [{"id": "w1", "name": "update_document", "args": {"html": "<p>native</p>"}, "argumentsText": '{"html":"<p>native</p>"}'}])],
                chapters=[{"id": "doc-2", "title": "Two", "content": "<p></p>"}], policy={"continueAfterWrites": False, "feedBackFailedWrites": True})
    asyncio.run(h.run.start())
    assert strip_diff_markup(h.fake.last_write("doc-1")) == "<p>native</p>"
    assert strip_diff_markup(h.fake.last_write("doc-2")) == "<p>tagged</p>"
    assert h.summary["strayMarkup"] == 0


# ── reminders, plan, ask_user ────────────────────────────────────────────────

def test_identical_steps_get_one_nudge_in_their_results():
    read = calls("r", ("c", "read_chapter", '{"chapter":"2"}'))
    h = Harness([read, read, read, read, DONE])
    asyncio.run(h.run.start())
    assert h.summary["steps"] == 5
    assert "<system-reminder>" not in h.requests[2][-1]["content"]
    assert "the same call (read_chapter)" in h.requests[3][-1]["content"]
    assert "<system-reminder>" not in h.requests[4][-1]["content"]
    assert h.run.identical_run_length() == 0  # the closing reply made no call


def test_argument_order_does_not_make_calls_differ():
    a = calls("r", ("c", "read_chapter", '{"chapter":"2","format":"text"}'))
    b = calls("r", ("c", "read_chapter", '{"format":"text","chapter":"2"}'))
    h = Harness([a, b, a, DONE])
    asyncio.run(h.run.start())
    assert "the same call" in h.requests[3][-1]["content"]


def test_long_reasoning_reminder_lands_one_step_later():
    read = calls("r", ("c", "read_chapter", '{"chapter":"2"}'))
    read.usage = {"promptTokens": 1, "completionTokens": 1, "reasoningTokens": 5000}
    later = calls("r", ("c2", "read_chapter", '{"chapter":"3"}'))
    h = Harness([read, later, DONE])
    h.run.long_reasoning_tokens = 1000
    asyncio.run(h.run.start())
    assert "reasoning trace (about 5,000 tokens)" not in h.requests[1][-1]["content"]
    assert "reasoning trace (about 5,000 tokens)" in h.requests[2][-1]["content"]


def test_plan_reminds_and_nudges_an_early_ending():
    from wc_agent.tools.plan import plan_tool
    plan_call = calls("Planning.", ("p", "plan", '{"items":[{"id":"a","title":"写第一章","status":"in_progress"},{"id":"b","title":"写第二章"}]}'))
    # Nothing was written, so "done" would be refused: the items are dropped.
    h = Harness([plan_call, text("写完了。\n<doc_status>unchanged</doc_status>"), calls("", ("p2", "plan", '{"items":[{"id":"a","status":"dropped"},{"id":"b","status":"dropped"}]}')), DONE],
                extra=[plan_tool])
    asyncio.run(h.run.start())
    assert "PLAN (0/2 done)" in h.requests[1][-1]["content"]
    nudge = h.requests[2][-1]["content"]
    assert "Your plan still has 2 unfinished items" in nudge and h.requests[2][-2]["content"] == "写完了。\n<doc_status>unchanged</doc_status>"
    assert h.summary["endReason"] == "answered" and h.summary["steps"] == 4
    assert [i["status"] for i in h.summary["plan"]] == ["dropped", "dropped"]


def test_a_turn_picks_up_the_previous_turns_unfinished_plan_and_is_nudged_to_go_on():
    # 2026-10-13: "continue" after a stopped turn had no plan; one chapter in, the model asked the user to say "continue".
    plan = [{"id": "c5", "title": "写第五章", "status": "done"}, {"id": "c6", "title": "写第六章", "status": "in_progress"}, {"id": "c7", "title": "写第七章", "status": "pending"}]
    h = Harness([text("你回复「继续」我就写。\n<doc_status>unchanged</doc_status>"), DONE, DONE], carried_plan=plan)
    asyncio.run(h.run.start())
    first = h.requests[0][-1]["content"]
    assert first.startswith("request\n\n<system-reminder>") and "Your plan from the previous turn is not finished" in first and "写第七章" in first
    assert "Your plan still has 2 unfinished items" in h.requests[1][-1]["content"]
    assert h.run.ctx.run.plan_baseline == {"c6": 0}
    # A finished plan carries nothing.
    h2 = Harness([DONE], carried_plan=[{"id": "a", "title": "x", "status": "done"}])
    asyncio.run(h2.run.start())
    assert h2.requests[0][-1]["content"] == "request" and h2.run.ctx.run.plan == []


def test_plan_says_when_a_done_chapter_is_still_empty():
    from wc_agent.tools.plan import plan_tool
    fake = FakeBook("<p>alpha</p>", chapters=[{"id": "doc-2", "title": "第二章", "content": "<p></p>"}])
    out = run_tool(plan_tool, {"items": [{"title": "写第二章", "status": "done"}]}, fake.ctx)
    assert out["ok"] and 'the chapter "第二章" is still empty' in out["content"]


def test_ask_user_waits_for_the_answer():
    from wc_agent.tools.ask_user import ask_user_tool
    h = Harness([calls("", ("q", "ask_user", '{"question":"Which?","options":["A (Recommended)","B"]}')), DONE], extra=[ask_user_tool])
    asyncio.run(h.run.start())
    assert h.summary is None and h.run.paused == {"reason": "question", "question": "Which?", "options": ["A (Recommended)", "B"]}
    asyncio.run(h.run.resume_with_answer("B"))
    assert h.summary["endReason"] == "answered"
    assert h.requests[1][-1] == {"role": "user", "content": "The user answered: B"}


def test_failed_search_names_the_nearest_paragraph():
    fake = FakeBook('<p>她说：“我们走吧。”他没有回头。</p><p>第二段。</p>')
    edit = DOCUMENT_WRITE_TOOLS[1]
    # The matcher forgives quotes alone; a changed punctuation mark makes it miss.
    out = run_tool(edit, {"edits": [{"search": '<p>她说："我们走吧。"他没有回头！</p>', "replace": "<p>x</p>"}]}, fake.ctx)
    assert out["ok"] is False
    assert "Nearest: ¶1 — copy this HTML exactly: <p>她说：“我们走吧。”他没有回头。</p>" in out["content"]
    assert "curly quotes" in out["content"]


def test_plan_refuses_done_on_a_write_item_with_nothing_written():
    from wc_agent.tools.plan import plan_tool
    h = Harness([
        calls("", ("p", "plan", '{"items":[{"id":"w","title":"改写第十四章","status":"in_progress"},{"id":"r","title":"对大纲接缝"}]}')),
        calls("", ("p2", "plan", '{"items":[{"id":"w","status":"done"},{"id":"r","status":"done"}],"merge":true}')),
        text("<canvas><p>written</p></canvas>"),
        calls("", ("p3", "plan", '{"items":[{"id":"w","status":"done"}],"merge":true}')),
        DONE,
    ], policy={"continueAfterWrites": True, "feedBackFailedWrites": True}, extra=[plan_tool])
    asyncio.run(h.run.start())
    refused = next(m["content"] for m in h.requests[2] if m.get("role") == "tool" and m.get("toolCallId") == "p2")
    assert '"改写第十四章" was marked done, but nothing has been written' in refused and "▶ 改写第十四章" in refused and "☑ 对大纲接缝" in refused
    accepted = next(m["content"] for m in h.requests[4] if m.get("role") == "tool" and m.get("toolCallId") == "p3")
    assert "☑ 改写第十四章" in accepted
    assert [i["status"] for i in h.summary["plan"]] == ["done", "done"]


def test_old_read_results_are_elided_when_the_prompt_outgrows_the_window():
    async def big_read(args, ctx, call):
        return {"ok": True, "content": f"TEXT OF {args.get('chapter')} " + "word " * 400, "trace": f"read {args.get('chapter')}"}
    h = Harness([calls("", ("c1", "read_chapter", '{"chapter":"1"}')), calls("", ("c2", "read_chapter", '{"chapter":"2"}')),
                 calls("", ("c3", "read_chapter", '{"chapter":"3"}')), DONE])
    h.registry = ToolRegistry([*DOCUMENT_WRITE_TOOLS, read_tool(big_read)])
    h.run.registry = h.registry
    h.run.prompt_token_limit = 1_000
    asyncio.run(h.run.start())
    assert len(h.requests) == 4
    third = {m.get("toolCallId"): m["content"] for m in h.requests[2] if m.get("role") == "tool"}
    assert third["c1"] == "[This result was elided to keep the conversation within the model's context window: read 1. Read it again if you need its text.]"
    assert "TEXT OF 2" in third["c2"]
    # The trace says what the elision costs the next step.
    assert any(t.startswith("🧹 elided 1 earlier result to stay within the context window (the next step re-reads ≈") and t.endswith("tokens uncached)")
               for t in h.summary["trace"])
    fourth = {m.get("toolCallId"): m["content"] for m in h.requests[3] if m.get("role") == "tool"}
    assert "elided" in fourth["c2"] and "TEXT OF 3" in fourth["c3"]
    snap = h.run.snapshot()
    assert snap["lastResultsStart"] > 0 and all(e["index"] < snap["lastResultsStart"] + 10 for e in snap["elidable"])


def test_a_run_within_its_limit_is_left_alone():
    async def big_read(args, ctx, call):
        return {"ok": True, "content": f"TEXT OF {args.get('chapter')} " + "word " * 400, "trace": f"read {args.get('chapter')}"}
    h = Harness([calls("", ("c1", "read_chapter", '{"chapter":"1"}')), calls("", ("c2", "read_chapter", '{"chapter":"2"}')), DONE])
    h.registry = ToolRegistry([*DOCUMENT_WRITE_TOOLS, read_tool(big_read)])
    h.run.registry = h.registry
    h.run.prompt_token_limit = 100_000
    asyncio.run(h.run.start())
    assert "TEXT OF 1" in next(m["content"] for m in h.requests[2] if m.get("toolCallId") == "c1")
    assert not any(t.startswith("🧹") for t in h.summary["trace"])


def test_a_message_sent_during_a_step_follows_its_results():
    h = Harness([])
    def first(messages):
        h.run.steer("把第二段删掉")
        return calls("", ("c", "read_chapter", '{"chapter":"1"}'))
    h.replies = [first, DONE]
    asyncio.run(h.run.start())
    assert len(h.requests) == 2
    last = h.requests[1][-1]
    assert last["role"] == "user" and "The user sent this message while you were working" in last["content"]
    assert last["content"].endswith("USER MESSAGE:\n把第二段删掉")
    assert h.requests[1][-2]["role"] == "tool"


def test_a_steer_marks_the_timeline_where_it_landed_for_the_chat_to_split_the_bubble():
    h = Harness([])
    def first(messages):
        h.run.steer("把第二段删掉", "user-9")
        return calls("读一下。", ("c", "read_chapter", '{"chapter":"1"}'))
    h.replies = [first, text("删好了。\n<doc_status>unchanged</doc_status>")]
    asyncio.run(h.run.start())
    kinds = [i["type"] for i in h.summary["timeline"]]
    # The step in flight stays before the mark; what the run did after it, after.
    assert kinds == ["text", "tool", "steer", "text"]
    assert h.summary["timeline"][2] == {"type": "steer", "messageIds": ["user-9"], "text": "把第二段删掉"}
    # A snapshot between steps keeps the id with the pending text.
    h2 = Harness([])
    h2.run.steer("稍后", "user-10")
    assert h2.run.snapshot()["pendingSteerIds"] == ["user-10"]


def test_a_message_sent_during_an_ending_reply_keeps_the_turn_going():
    h = Harness([], policy={"continueAfterWrites": True, "feedBackFailedWrites": True})
    def first(messages):
        h.run.steer("再写一段结尾")
        return text("写好了。\n<doc_status>unchanged</doc_status>")
    h.replies = [first, text("<canvas><p>结尾</p></canvas>\n<doc_status>updated</doc_status>"), DONE]
    asyncio.run(h.run.start())
    assert len(h.requests) == 3
    assert h.requests[1][-2]["content"] == "写好了。\n<doc_status>unchanged</doc_status>"
    assert h.requests[1][-1]["content"].endswith("USER MESSAGE:\n再写一段结尾")
    assert strip_diff_markup(h.fake.last_write("doc-1")) == "<p>结尾</p>"
    assert h.summary["endReason"] == "answered" and h.summary["steps"] == 3


def test_a_pending_message_survives_a_snapshot():
    h = Harness([DONE])
    h.run.steer("x")
    snap = h.run.snapshot()
    assert snap["pendingSteers"] == ["x"]
    other = Harness([DONE])
    other.run.restore(snap, lambda doc_id: None)
    assert other.run.pending_steers == ["x"]


def test_a_claim_without_a_write_gets_the_editors_facts_once():
    h = Harness([calls("", ("c", "read_chapter", '{"chapter":"1"}')), text("I have rewritten chapter 1 as you asked."),
                 text("I have rewritten it again."), text("Nothing was changed, sorry.")], write_protocol="tools")
    asyncio.run(h.run.start())
    assert len(h.requests) == 3
    nudge = h.requests[2][-1]["content"]
    assert "Editor facts for this turn: 0 writes reached the book," in nudge and "0 plan items left." in nudge
    assert "Either make the write now" in nudge
    assert h.summary["endReason"] == "answered" and "I have rewritten it again." in h.summary["chatText"]


def test_a_claim_after_a_write_is_not_nudged():
    h = Harness([text("<canvas><p>beta</p></canvas>"), text("I have rewritten chapter 1.")], write_protocol="tools",
                policy={"continueAfterWrites": True, "feedBackFailedWrites": True})
    asyncio.run(h.run.start())
    assert len(h.requests) == 2 and h.summary["endReason"] == "answered"


def test_html_read_without_an_edit_is_nudged_once_at_the_ending():
    async def html_read(args, ctx, call):
        return {"ok": True, "content": "<p>¶88</p>", "trace": f"read #1 ¶88 ({args.get('format')})"}
    h = Harness([calls("", ("c", "read_chapter", '{"chapter":"1","format":"html"}')), DONE, DONE])
    h.registry = ToolRegistry([*DOCUMENT_WRITE_TOOLS, read_tool(html_read)])
    h.run.registry = h.registry
    asyncio.run(h.run.start())
    assert len(h.requests) == 3
    assert "Your last read of a chapter's HTML (read #1 ¶88 (html)) is the step before an edit" in h.requests[2][-1]["content"]
    assert h.summary["endReason"] == "answered"


# ── tools that save steps (agentic_chat_loop.md §0.11) ──────────────────────

from wc_agent.tools.book_reads import grep_tool  # noqa: E402
from wc_agent.tools.document_writes import edit_paragraphs_tool  # noqa: E402
from wc_agent.tools.plan import plan_tool  # noqa: E402
from wc_text.context_ledger import hash_content  # noqa: E402

_PLAIN = "<h1>第二章</h1><p>阿青推开门。</p><p>屋里没有人。</p><p>她坐了下来。</p>"
_FORMATTED = "<p>阿青推开<em>门</em>。</p><p>屋里没有人。</p>"


def _efficiency_book() -> FakeBook:
    return FakeBook("<p>start</p>", chapters=[{"id": "doc-2", "title": "第二章", "content": _PLAIN},
                                              {"id": "doc-3", "title": "第三章", "content": _FORMATTED},
                                              {"id": "doc-4", "title": "第四章", "content": "<p>一。</p><p>二。</p><p>三。</p><p>四。</p><p>五。</p>"}])


def test_read_chapter_parts_read_several_places_in_one_call():
    fake = _efficiency_book()
    out = run_tool(read_chapter_tool, {"parts": [{"chapter": "2", "paragraphs": "2-3"}, {"chapter": "4", "paragraphs": "4-5"}, {"chapter": "4", "paragraphs": "1"}]}, fake.ctx)
    assert out["ok"] and "¶2 阿青推开门。" in out["content"] and "¶4 四。\n¶5 五。" in out["content"] and "¶1 一。" in out["content"]
    assert out["trace"] == '📖 read #2 "第二章" ¶2–3 (0.0k, text), #4 "第四章" ¶4–5 (0.0k, text), #4 "第四章" ¶1–1 (0.0k, text)'
    assert "at most 12 parts" in run_tool(read_chapter_tool, {"parts": [{"chapter": "2"}] * 13}, fake.ctx)["content"]


def test_grep_patterns_are_reported_one_by_one():
    fake = _efficiency_book()
    out = run_tool(grep_tool, {"patterns": ["阿青", "五|六", "不存在"]}, fake.ctx)
    assert "=== /阿青/ ===\n2 match(es) in 2 chapter(s):" in out["content"]
    assert '=== /五|六/ ===\n1 match(es) in 1 chapter(s):\n#4 "第四章" ¶5: 五。' in out["content"]
    assert out["trace"] == "🔎 grep 3 patterns in the whole book → /阿青/ 2, /五|六/ 1, /不存在/ 0 (3 in all)"
    single = run_tool(grep_tool, {"pattern": "五"}, fake.ctx)
    assert single["content"] == '1 match(es) in 1 chapter(s):\n#4 "第四章" ¶5: 五。'


def test_grep_takes_many_cheap_patterns_in_one_call():
    """2026-10-24: 15 exact sentences, one match each, were refused by a 10-pattern cap."""
    fake = _efficiency_book()
    out = run_tool(grep_tool, {"patterns": ["阿青"] + [f"无此{i}" for i in range(14)]}, fake.ctx)
    assert out["ok"] and "=== /无此13/ ===" in out["content"] and "Not shown" not in out["content"]
    assert out["trace"].startswith("🔎 grep 15 patterns in the whole book")


def test_grep_past_its_size_cap_names_the_patterns_left_out(monkeypatch):
    import wc_agent.tools.book_reads as book_reads
    monkeypatch.setattr(book_reads, "GREP_OUTPUT_CAP", 120)
    fake = _efficiency_book()
    out = run_tool(grep_tool, {"patterns": ["阿青", "屋里", "五", "无此"]}, fake.ctx)
    assert out["ok"] and "=== /阿青/ ===" in out["content"]
    assert "send them in the next call:" in out["content"] and "/无此/" in out["content"].split("send them in the next call:")[1]
    assert "not shown (size limit)" in out["trace"]
    # One pattern alone past the cap: its snippets are cut, with how to narrow it.
    monkeypatch.setattr(book_reads, "GREP_OUTPUT_CAP", 40)
    single = run_tool(grep_tool, {"pattern": "。"}, fake.ctx)
    assert single["ok"] and "more snippet(s) not shown" in single["content"]


def test_grep_past_its_pattern_bound_names_the_rest():
    fake = _efficiency_book()
    out = run_tool(grep_tool, {"patterns": [f"无此{i}" for i in range(55)]}, fake.ctx)
    assert "these 5 were left out; send them in the next call: /无此50/" in out["content"]
    assert out["trace"].endswith(" · 5 patterns left for the next call")


def test_a_plain_chapter_is_rewritten_after_its_whole_text_was_read():
    fake = _efficiency_book()
    update = DOCUMENT_WRITE_TOOLS[0]
    assert run_tool(update, {"chapter": "2", "html": "<p>x</p>"}, fake.ctx)["trace"] == '⛔ rewrite of #2 "第二章" refused — not read yet'
    run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)
    assert run_tool(update, {"chapter": "2", "html": "<p>新的。</p>"}, fake.ctx)["ok"]
    fake2 = _efficiency_book()
    run_tool(read_chapter_tool, {"chapters": ["3"]}, fake2.ctx)
    out = run_tool(update, {"chapter": "3", "html": "<p>x</p>"}, fake2.ctx)
    assert not out["ok"] and "formatting or images a text read does not show" in out["content"]


def test_text_in_context_at_the_start_counts_until_the_chapter_changes():
    fake = _efficiency_book()
    fake.ctx.run.text_seen["doc-2"] = hash_content(_PLAIN)
    fake.user_edits("doc-2", _PLAIN + "<p>用户加的。</p>")
    assert not run_tool(DOCUMENT_WRITE_TOOLS[0], {"chapter": "2", "html": "<p>x</p>"}, fake.ctx)["ok"]


def test_edit_paragraphs_changes_paragraphs_by_number_and_refuses_a_stale_anchor():
    fake = _efficiency_book()
    out = run_tool(edit_paragraphs_tool, {"chapter": "4", "edits": [
        {"paragraph": 2, "action": "replace", "html": "<p>贰。</p>", "starts_with": "二"},
        {"paragraph": 4, "action": "delete", "starts_with": "¶4 四"},
        {"paragraph": 5, "action": "insert_after", "html": "六。", "starts_with": "五"}]}, fake.ctx)
    assert out["ok"] and out["trace"] == '✏️ edited #4 "第四章" by paragraph (3 changes)'
    assert strip_diff_markup(fake.last_write("doc-4")) == "<p>一。</p><p>贰。</p><p>三。</p><p>五。</p><p>六。</p>"
    fake2 = _efficiency_book()
    stale = run_tool(edit_paragraphs_tool, {"chapter": "4", "edits": [{"paragraph": 1, "action": "replace", "html": "x", "starts_with": "一"},
                                                                      {"paragraph": 3, "action": "delete", "starts_with": "二"}]}, fake2.ctx)
    # The edit that fits applies; the stale one comes back.
    assert stale["ok"] and "NOT applied (1)" in stale["content"] and "¶3 does not start with what you gave; it now reads: 三。" in stale["content"]
    assert strip_diff_markup(fake2.last_write("doc-4")) == "<p>x</p><p>二。</p><p>三。</p><p>四。</p><p>五。</p>"
    assert stale["trace"] == '✏️ edited #4 "第四章" by paragraph (1 change; 1 not applied)'
    fake3 = _efficiency_book()
    none = run_tool(edit_paragraphs_tool, {"chapter": "4", "edits": [{"paragraph": 3, "action": "delete", "starts_with": "二"}]}, fake3.ctx)
    assert not none["ok"] and fake3.last_write("doc-4") is None and "1 anchor out of date" in none["trace"]


def test_a_card_is_read_and_edited_entry_by_entry_and_a_slipped_anchor_lands():
    card = "<h1>人物卡</h1><h2>林霜</h2><ul><li><p>身份：港务局引航员，三级。</p></li><li><p>资质：甲等。</p></li></ul>"
    fake = FakeBook("<p>start</p>", chapters=[{"id": "doc-2", "title": "人物卡", "content": card}])
    read = run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)
    assert "¶3 • 身份：港务局引航员，三级。" in read["content"] and 'read it with format="html" first' in read["content"]
    out = run_tool(edit_paragraphs_tool, {"chapter": "2", "edits": [
        {"paragraph": 3, "action": "replace", "html": "<p>身份：港务局引航员，四级。</p>", "starts_with": "身份：港务局引杭员"}]}, fake.ctx)
    assert out["ok"]
    assert strip_diff_markup(fake.last_write("doc-2")) == "<h1>人物卡</h1><h2>林霜</h2><ul><li><p>身份：港务局引航员，四级。</p></li><li><p>资质：甲等。</p></li></ul>"


def test_read_takes_a_list_of_scattered_paragraphs():
    fake = _efficiency_book()
    out = run_tool(read_chapter_tool, {"chapters": ["4"], "paragraphs": "1,3-4，5"}, fake.ctx)
    assert out["ok"] and "¶1 一。" in out["content"] and "¶3 三。\n¶4 四。" in out["content"] and "¶5 五。" in out["content"] and "¶2 二。" not in out["content"]


def test_plan_done_on_a_write_marks_the_item_and_starts_the_next():
    fake = _efficiency_book()
    run_tool(plan_tool, {"items": [{"id": "ch2", "title": "改写第二章", "status": "in_progress"}, {"id": "ch4", "title": "改写第四章"}]}, fake.ctx)
    refused = run_tool(DOCUMENT_WRITE_TOOLS[0], {"chapter": "2", "html": "<p>新。</p>", "plan_done": "ch2"}, fake.ctx)
    assert not refused["ok"] and fake.ctx.run.plan[0]["status"] == "in_progress"
    run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)
    out = run_tool(DOCUMENT_WRITE_TOOLS[0], {"chapter": "2", "html": "<p>新。</p>", "plan_done": ["ch2"]}, fake.ctx)
    assert out["ok"] and out["trace"].endswith("· 📋 plan 1/2") and "☑ 改写第二章\n▶ 改写第四章" in out["content"]
    out = run_tool(edit_paragraphs_tool, {"chapter": "4", "edits": [{"paragraph": 1, "action": "delete", "starts_with": "一"}], "plan_done": "ch4, nope"}, fake.ctx)
    assert '(plan_done: "nope" is not in the plan.)' in out["content"]
    assert [i["status"] for i in fake.ctx.run.plan] == ["done", "done"]


def test_eight_lookup_only_steps_get_one_reminder():
    reads = [calls("", (f"c{i}", "read_chapter", json.dumps({"chapter": "1", "paragraphs": str(i + 1)}))) for i in range(10)]
    h = Harness([*reads, DONE], budgets={"maxSteps": 0})
    asyncio.run(h.run.start())
    said = [any("only looked things up" in (m.get("content") or "") for m in r) for r in h.requests]
    assert said.index(True) == 8
    assert "The last 8 steps only looked things up and wrote nothing" in h.requests[8][-1]["content"]
    assert sum("only looked things up" in (m.get("content") or "") for m in h.requests[10]) == 1


# ── whole rewrites that would lose text ──────────────────────────────────────

from wc_agent.tools.document_writes import update_document_tool  # noqa: E402


def _timeline_entry(n: int) -> str:
    return f"<p>第{n}章：{'事件' * 20}，人物{n}登场。</p>"


_TIMELINE = "<h1>时间线</h1>" + "".join(_timeline_entry(i + 1) for i in range(20))


def test_a_shrinking_rewrite_is_held_back_once_then_applied():
    fake = FakeBook("<p>start</p>", chapters=[{"id": "doc-2", "title": "时间线", "content": _TIMELINE}])
    run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)
    shorter = "<h1>时间线</h1>" + "".join(_timeline_entry(i + 1) for i in range(14)) + "<p>第21章：新事件。</p>"
    first = run_tool(update_document_tool, {"chapter": "2", "html": shorter}, fake.ctx)
    assert not first["ok"] and "was NOT applied" in first["content"] and "edit_paragraphs" in first["content"]
    assert "held back — would drop" in first["trace"]
    assert strip_diff_markup(fake.last_write("doc-2") or _TIMELINE) == _TIMELINE
    second = run_tool(update_document_tool, {"chapter": "2", "html": shorter}, fake.ctx)
    assert second["ok"] and "第21章：新事件" in strip_diff_markup(fake.last_write("doc-2"))


def test_a_folded_timeline_is_refused_as_elided():
    fake = FakeBook("<p>start</p>", chapters=[{"id": "doc-2", "title": "时间线", "content": _TIMELINE}])
    run_tool(read_chapter_tool, {"chapters": ["2"]}, fake.ctx)
    folded = "<h1>时间线</h1><p>第1–19章：见原条目。</p>" + "".join(_timeline_entry(i + 20) for i in range(30))
    out = run_tool(update_document_tool, {"chapter": "2", "html": folded}, fake.ctx)
    assert not out["ok"] and "not applied (elided)" in out["trace"]


# ── moving text the book already has (paragraph_moves) ───────────────────────
from wc_agent.tools.paragraph_moves import copy_paragraphs_tool, move_chapter_tool  # noqa: E402


def _chapter(fake, doc_id):
    return next(c for c in fake.book if c["id"] == doc_id)


def _titles(fake):
    return [c["title"] for c in fake.book]


def test_a_chapter_is_split_into_a_new_chapter_right_after_it_without_re_typing():
    """2026-10-24: a request to split a chapter re-typed ~20,000 characters to move text."""
    fake = _efficiency_book()
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "4", "paragraphs": "3-5", "starts_with": "三", "last_starts_with": "五",
                                          "new_chapter": "第四章（下）"}, fake.ctx)
    assert out["ok"], out
    assert _titles(fake) == ["Chapter 1", "第二章", "第三章", "第四章", "第四章（下）"]
    assert strip_diff_markup(fake.last_write("new-1")) == "<p>三。</p><p>四。</p><p>五。</p>"
    assert strip_diff_markup(fake.last_write("doc-4")) == "<p>一。</p><p>二。</p>"
    assert "diff-deletion" in fake.last_write("doc-4")  # the move is reviewable at the source
    assert out["trace"] == '✂️ moved ¶3–5 of #4 "第四章" → new #5 "第四章（下）" (3 ¶)'


def test_a_new_chapter_can_be_placed_anywhere_and_the_source_keeps_its_place():
    fake = _efficiency_book()
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "4", "paragraphs": "1", "starts_with": "一", "new_chapter": "序", "after_chapter": 0}, fake.ctx)
    assert out["ok"], out
    assert _titles(fake) == ["序", "Chapter 1", "第二章", "第三章", "第四章"]
    assert "#1 \"序\"" in out["content"] and "moved down by one" in out["content"]
    assert strip_diff_markup(fake.last_write("doc-4")) == "<p>二。</p><p>三。</p><p>四。</p><p>五。</p>"


def test_paragraphs_are_moved_into_another_chapter_after_a_named_paragraph():
    fake = _efficiency_book()
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "4", "paragraphs": "4-5", "starts_with": "四", "last_starts_with": "五",
                                          "to_chapter": "2", "after_paragraph": 2, "after_starts_with": "阿青"}, fake.ctx)
    assert out["ok"], out
    assert strip_diff_markup(fake.last_write("doc-2")) == "<h1>第二章</h1><p>阿青推开门。</p><p>四。</p><p>五。</p><p>屋里没有人。</p><p>她坐了下来。</p>"
    assert strip_diff_markup(fake.last_write("doc-4")) == "<p>一。</p><p>二。</p><p>三。</p>"


def test_a_copy_leaves_the_source_as_it_was_and_goes_to_the_end_by_default():
    fake = _efficiency_book()
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "4", "paragraphs": "1-2", "starts_with": "一", "last_starts_with": "二",
                                          "to_chapter": "2", "move": False}, fake.ctx)
    assert out["ok"], out
    assert strip_diff_markup(fake.last_write("doc-2")).endswith("<p>她坐了下来。</p><p>一。</p><p>二。</p>")
    assert fake.last_write("doc-4") is None
    assert out["trace"].startswith("📄 copied")


def test_paragraphs_are_moved_within_their_own_chapter():
    fake = _efficiency_book()
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "4", "paragraphs": "1", "starts_with": "一", "to_chapter": "4"}, fake.ctx)
    assert out["ok"], out
    assert strip_diff_markup(fake.last_write("doc-4")) == "<p>二。</p><p>三。</p><p>四。</p><p>五。</p><p>一。</p>"
    nothing = run_tool(copy_paragraphs_tool, {"from_chapter": "4", "paragraphs": "2-3", "starts_with": "三", "last_starts_with": "四",
                                              "to_chapter": "4", "after_paragraph": 2, "after_starts_with": "三"}, fake.ctx)
    assert nothing["ok"] is False and "already there" in nothing["content"]


def test_a_stale_anchor_changes_nothing_and_shows_what_the_paragraph_reads():
    fake = _efficiency_book()
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "4", "paragraphs": "2-3", "starts_with": "二", "last_starts_with": "五",
                                          "new_chapter": "x"}, fake.ctx)
    assert out["ok"] is False and "¶3 does not start with what you gave; it now reads: 三。" in out["content"]
    assert fake.created == 0 and fake.last_write("doc-4") is None


def test_a_move_into_a_chapter_the_user_changed_meanwhile_is_refused():
    fake = _efficiency_book()
    run_tool(read_chapter_tool, {"chapters": ["2"], "format": "html"}, fake.ctx)
    fake.user_edits("doc-2", "<p>用户改过。</p>")
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "4", "paragraphs": "1", "starts_with": "一", "to_chapter": "2"}, fake.ctx)
    assert out["ok"] is False and "edited by the user meanwhile" in out["trace"]
    assert fake.last_write("doc-4") is None


def test_move_chapter_reorders_without_touching_text():
    fake = _efficiency_book()
    out = run_tool(move_chapter_tool, {"chapter": "4", "position": 2}, fake.ctx)
    assert out["ok"] and _titles(fake) == ["Chapter 1", "第四章", "第二章", "第三章"]
    assert "between #2 and #4 moved down by one" in out["content"]
    assert fake.last_write("doc-4") is None
    assert run_tool(move_chapter_tool, {"chapter": "2", "position": 2}, fake.ctx)["content"].endswith("is already at #2.")


def test_edit_paragraphs_deletes_a_range_in_one_edit():
    fake = _efficiency_book()
    out = run_tool(edit_paragraphs_tool, {"chapter": "4", "edits": [{"paragraph": 2, "action": "delete", "starts_with": "二",
                                                                     "through": 4, "through_starts_with": "四"}]}, fake.ctx)
    assert out["ok"], out
    assert strip_diff_markup(fake.last_write("doc-4")) == "<p>一。</p><p>五。</p>"
    stale = run_tool(edit_paragraphs_tool, {"chapter": "4", "edits": [{"paragraph": 1, "action": "delete", "starts_with": "一",
                                                                       "through": 2, "through_starts_with": "二"}]}, fake.ctx)
    assert stale["ok"] is False and "¶2 (the end of the range) does not start with what you gave" in stale["content"]


def test_moved_paragraphs_keep_their_images_and_list_entries_exactly():
    img = '<p><img src="data:image/png;base64,AAAA" alt="a"></p>'
    fake = FakeBook("<p>start</p>", chapters=[{"id": "doc-2", "title": "第二章", "content": f"<p>甲。</p>{img}<ul><li><p>乙</p></li><li><p>丙</p></li></ul><p>丁。</p>"}])
    out = run_tool(copy_paragraphs_tool, {"from_chapter": "2", "paragraphs": "2-4", "starts_with": "[image]", "last_starts_with": "丙",
                                          "new_chapter": "第三章"}, fake.ctx)
    assert out["ok"], out
    assert strip_diff_markup(fake.last_write("new-1")) == f"{img}<ul><li><p>乙</p></li><li><p>丙</p></li></ul>"
    assert strip_diff_markup(fake.last_write("doc-2")) == "<p>甲。</p><p>丁。</p>"


def test_a_selection_that_is_gone_does_not_lock_the_chapter_for_the_rest_of_the_turn():
    """2026-10-25: a stale selection (already rewritten last turn) went nowhere, and then edit_paragraphs,
    replace_text and a whole rewrite were all refused for ten steps."""
    from wc_agent.tools.document_writes import replace_text_tool
    fake = FakeBook("<p>甲。</p><p>乙。</p>", selected_text="早已不在这一章里的旧选区。")
    out = run_tool(DOCUMENT_WRITE_TOOLS[2], {"html": "<p>新的。</p>"}, fake.ctx)
    assert out["ok"] is False and "the selection is gone" in out["trace"] and "not blocked" in out["content"]
    assert fake.ctx.run.selection_attempted is False
    edited = run_tool(edit_paragraphs_tool, {"edits": [{"paragraph": 2, "action": "replace", "starts_with": "乙", "html": "<p>丙。</p>"}]}, fake.ctx)
    assert edited["ok"], edited
    replaced = run_tool(replace_text_tool, {"find": "甲", "replace": "丁", "expect": 1, "chapters": ["1"]}, fake.ctx)
    assert replaced["ok"], replaced


def test_replace_text_says_a_locked_chapter_was_not_searched():
    from wc_agent.tools.document_writes import replace_text_tool
    fake = FakeBook("<p>甲。</p>", selected_text="甲。")
    fake.ctx.run.selection_attempted = True  # a selection rewrite landed or is in flight this turn
    out = run_tool(replace_text_tool, {"find": "甲", "replace": "丁", "expect": 1, "chapters": ["1"]}, fake.ctx)
    assert out["ok"] is False and "not searched" in out["trace"] and "selection rewrite" in out["content"]
