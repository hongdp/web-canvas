"""view_images (2026-10-18): the agent looks at a chapter's pictures — the tool on a fake book,
the four wire formats carrying a result's images, and the compaction weighing them."""
import asyncio

from test_agent import FakeBook, run_tool
from wc_agent.tools.view_images import VIEW_PER_CALL, VIEW_PER_RUN, chapter_images, supports_vision, view_images_tool
from wc_providers.anthropic import build_anthropic_request
from wc_providers.gemini import build_gemini_request
from wc_providers.openai_compat import build_openai_request
from wc_providers.responses import build_responses_request
from wc_text.run_compaction import IMAGE_TOKENS, plan_elisions, prompt_tokens

PHOTO = "data:image/jpeg;base64,QUJD"


class FakeVision:
    def __init__(self, fail=()):
        self.asked = []
        self.fail = set(fail)

    async def view(self, srcs):
        self.asked.append(list(srcs))
        return [{"error": "HTTP 404"} if s in self.fail else {"dataUrl": f"data:image/jpeg;base64,{s[-1]}"} for s in srcs]


def _book(n_images=3, fail=()):
    body = "<h1>第一章</h1><p>她推开门。</p>" + "".join(f'<p><img src="https://pics.example/{i}"></p><p>第{i}段。</p>' for i in range(1, n_images + 1))
    fake = FakeBook(body)
    fake.ctx.vision = FakeVision(fail)
    return fake


def test_chapter_images_are_numbered_with_the_paragraph_a_read_shows():
    html = '<p>a</p><p><img src="x1"></p><p>b <img src=\'x2\'></p><p><ins class="diff-addition" data-diff-id="d">new</ins></p>'
    assert [{k: i[k] for k in ("n", "paragraph", "src")} for i in chapter_images(html)] == [{"n": 1, "paragraph": 2, "src": "x1"}, {"n": 2, "paragraph": 3, "src": "x2"}]
    assert chapter_images(html)[1]["tag"] == '<img src="x2">'  # as the paragraph split re-spells it


def test_viewing_sends_the_pictures_on_the_result_in_order():
    fake = _book(3)
    out = run_tool(view_images_tool, {}, fake.ctx)
    assert out["ok"] and out["images"] == ["data:image/jpeg;base64,1", "data:image/jpeg;base64,2", "data:image/jpeg;base64,3"]
    assert "has 3 images" in out["content"] and "[Image 2] = image 2 of 3, in ¶5" in out["content"]
    assert out["trace"] == '🖼️ viewed 3 images of #1 "Chapter 1" (#1, #2, #3)'


def test_pictures_are_chosen_by_paragraph_or_by_number_and_failures_are_named():
    fake = _book(4, fail=("https://pics.example/3",))
    out = run_tool(view_images_tool, {"paragraphs": "5-7"}, fake.ctx)
    assert fake.ctx.vision.asked[-1] == ["https://pics.example/2", "https://pics.example/3"]
    assert len(out["images"]) == 1 and "image 3 (¶7, token " in out["content"] and "not shown — HTTP 404" in out["content"]
    out = run_tool(view_images_tool, {"images": [4]}, fake.ctx)
    assert fake.ctx.vision.asked[-1] == ["https://pics.example/4"]
    out = run_tool(view_images_tool, {"images": [9]}, fake.ctx)
    assert out["ok"] is False and "there is no image 9" in out["content"]
    out = run_tool(view_images_tool, {"paragraphs": "1-2"}, fake.ctx)
    assert out["ok"] is False and "Its images are in ¶3, ¶5, ¶7, ¶9" in out["content"]


def test_a_call_and_a_turn_are_capped():
    fake = _book(VIEW_PER_CALL + 2)
    out = run_tool(view_images_tool, {}, fake.ctx)
    assert len(out["images"]) == VIEW_PER_CALL and "2 more of those are not shown" in out["content"]
    fake.ctx.run.images_viewed = VIEW_PER_RUN
    out = run_tool(view_images_tool, {"images": [7]}, fake.ctx)
    assert out["ok"] is False and "image limit" in out["trace"]


def test_a_chapter_without_pictures_says_so_and_the_tool_needs_a_vision_model():
    fake = FakeBook("<p>只有文字。</p>")
    assert not view_images_tool.is_available(fake.ctx)
    fake.ctx.vision = FakeVision()
    assert view_images_tool.is_available(fake.ctx)
    assert run_tool(view_images_tool, {}, fake.ctx)["content"] == '#1 "Chapter 1" has no images.'
    assert supports_vision("anthropic", "claude-opus-5-5") and supports_vision("grok", "grok-4.6")
    assert not supports_vision("ollama", "qwen3:14b") and not supports_vision("moonshot", "kimi-k3")


# ── the wire formats ─────────────────────────────────────────────────────────

CALL = {"role": "assistant", "content": "", "toolCalls": [{"id": "c1", "name": "view_images", "argumentsText": "{}"}]}
RESULT = {"role": "tool", "toolCallId": "c1", "name": "view_images", "content": "Shown below", "images": [PHOTO]}
TURN = [{"role": "system", "content": "sys"}, {"role": "user", "content": "看图"}, CALL, RESULT, {"role": "user", "content": "next"}]


def test_anthropic_puts_the_pictures_inside_the_tool_result():
    _, _, body = build_anthropic_request({"apiKey": "k", "model": "claude-sonnet-5-5"}, TURN)
    block = body["messages"][2]["content"][0]
    assert block["type"] == "tool_result" and block["content"][0] == {"type": "text", "text": "Shown below"}
    assert block["content"][2] == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}


def test_responses_follow_the_outputs_with_a_user_message_of_pictures():
    _, _, body = build_responses_request({"apiKey": "k", "model": "grok-4.6", "baseUrl": "https://api.x.ai/v1"}, TURN)
    kinds = [i.get("type") or i.get("role") for i in body["input"]]
    assert kinds == ["system", "user", "function_call", "function_call_output", "user", "user"]
    assert body["input"][4]["content"][1] == {"type": "input_image", "image_url": PHOTO}


def test_gemini_puts_the_pictures_after_the_function_responses():
    _, _, body = build_gemini_request({"apiKey": "k", "model": "gemini-4-argon", "baseUrl": "https://g"}, TURN)
    parts = body["contents"][2]["parts"]
    assert "functionResponse" in parts[0] and parts[2] == {"inlineData": {"mimeType": "image/jpeg", "data": "QUJD"}}


def test_chat_completions_follow_the_tool_messages_with_a_user_message_of_pictures():
    _, _, body = build_openai_request({"apiKey": "k", "model": "m", "baseUrl": "https://x"}, TURN, "openai")
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user", "user"]
    assert body["messages"][4]["content"][1] == {"type": "image_url", "image_url": {"url": PHOTO}}


def test_compaction_weighs_pictures_and_drops_them_with_an_elided_result():
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}, CALL, RESULT, CALL, {**RESULT, "toolCallId": "c2"}]
    assert prompt_tokens(messages) >= 2 * IMAGE_TOKENS
    plan = plan_elisions(messages, [{"index": 3, "trace": "viewed"}], 2_000, 5)
    assert plan["elided"] == ["viewed"] and "images" not in plan["messages"][3]
    assert plan["messages"][5]["images"] == [PHOTO]


# ── through the run engine: tokens to copy a picture, and numbers that hold across turns ──

import json  # noqa: E402
import re  # noqa: E402

import server_content  # noqa: E402
import server_context  # noqa: E402
import server_generation  # noqa: E402
import server_web  # noqa: E402
from test_runs import Scripted, book, content_of, request, settle  # noqa: E402,F401
from wc_text.diff_resolution import resolve_diff_markup_in_html  # noqa: E402

A = '<img src="https://pics.example/a.jpg">'
B = '<img src="https://pics.example/b.jpg">'
C = '<img src="https://pics.example/c.jpg">'


class Capturing(Scripted):
    """Keeps every tool result the model was sent."""

    def __init__(self, replies):
        super().__init__(replies)
        self.results = []

    async def __call__(self, job, provider, config, messages):
        self.results = [m for m in messages if m.get("role") == "tool"]
        return await super().__call__(job, provider, config, messages)


def _fake_view(monkeypatch):
    async def view(username, srcs):
        return [{"dataUrl": "data:image/jpeg;base64,QUJD"} for _ in srcs]
    monkeypatch.setattr(server_web, "view_images", view)


def _run(engine, replies, monkeypatch, prompt="看图"):
    provider = Capturing(replies)
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = engine.submit("alice", "book-1", request(prompt))
        await settle(run)
        return run
    return asyncio.run(main()), provider


def test_a_viewed_picture_is_copied_into_another_chapter_by_its_token_and_reject_takes_it_out(book, monkeypatch):
    _fake_view(monkeypatch)
    server_content.save_document_content("alice", "book-1", "doc-1", f"<p>开头。</p><p>{A}</p>")
    view = {"text": "", "calls": [("v1", "view_images", json.dumps({"chapter": "1"}))]}
    read = {"text": "", "calls": [("r1", "read", json.dumps({"chapters": ["2"]}))]}
    write = {"text": "", "calls": [("w1", "update_document", json.dumps(
        {"chapter": "2", "html": "<p>扩写。</p><p>{{IMAGE_PLACEHOLDER_0}}</p>"}, ensure_ascii=False))]}
    run, provider = _run(book, [view, read, write, "好了。"], monkeypatch)
    assert run.status == "done", run.error
    shown = provider.results[0]
    assert shown["images"] == ["data:image/jpeg;base64,QUJD"] and "its token: {{IMAGE_PLACEHOLDER_0}}" in shown["content"]
    stored = content_of("doc-2")
    assert A in stored and '<ins class="diff-addition"' in stored
    assert A not in resolve_diff_markup_in_html(stored, "reject")
    assert A in content_of("doc-1")  # a copy: the original stays


def test_a_picture_keeps_its_number_when_the_book_gains_one_before_it(book, monkeypatch):
    """Numbers per run followed the order images were met: a new first picture renumbered every other one."""
    _fake_view(monkeypatch)
    server_content.save_document_content("alice", "book-1", "doc-1", f"<p>{A}</p>")
    server_content.save_document_content("alice", "book-1", "doc-2", f"<p>{B}</p>")
    view = lambda ch: {"text": "", "calls": [("v", "view_images", json.dumps({"chapter": ch}))]}  # noqa: E731
    run, provider = _run(book, [view("2"), "好。"], monkeypatch, "一")
    assert "its token: {{IMAGE_PLACEHOLDER_1}}" in provider.results[0]["content"]
    server_content.save_document_content("alice", "book-1", "doc-1", f"<p>{C}</p><p>{A}</p>")
    run, provider = _run(book, [view("1"), "好。"], monkeypatch, "二")
    content = provider.results[-1]["content"]   # the first turn's result is replayed ahead of it
    assert "image 1 of 2, in ¶1 — its token: {{IMAGE_PLACEHOLDER_2}}" in content   # C, new: the next number
    assert "image 2 of 2, in ¶2 — its token: {{IMAGE_PLACEHOLDER_0}}" in content   # A, as before
    assert len(server_context.load_image_numbers("alice", "book-1")) == 3


def test_a_number_whose_picture_left_the_book_keeps_its_slot():
    keys = [server_context.image_key(A), server_context.image_key(B)]
    registry = server_context.stable_image_registry(keys, [{"content": f"<p>{B}</p><p>{C}</p>"}])
    assert [e["tag"] for e in registry] == ["", B, C]
    assert server_context.registry_keys(registry, keys) == [keys[0], keys[1], server_context.image_key(C)]


def test_a_re_spelled_tag_is_the_picture_the_book_already_numbered(book, monkeypatch):
    _fake_view(monkeypatch)
    server_content.save_document_content("alice", "book-1", "doc-1", "<p><img src='https://pics.example/a.jpg' alt=\"x\"></p>")
    run, provider = _run(book, [{"text": "", "calls": [("v", "view_images", json.dumps({}))]}, "好。"], monkeypatch)
    assert "its token: {{IMAGE_PLACEHOLDER_0}}" in provider.results[-1]["content"]
    assert len(server_context.load_image_numbers("alice", "book-1")) == 1


def test_a_full_size_picture_is_not_written_back_as_its_thumbnail(book, monkeypatch):
    """2026-10-25: a small thumbnail of a picture sits in an earlier chapter, the full-size picture in a later one;
    keyed by src, the full picture's token restored to the thumbnail and a rewrite shrank every picture."""
    thumb = '<img src="https://pics.example/a.jpg" style="width: 60px; height: 60px; object-fit: cover;">'
    server_content.save_document_content("alice", "book-1", "doc-1", f"<h2>Index</h2><div>{thumb}</div>")
    server_content.save_document_content("alice", "book-1", "doc-2", f"<p>正文。</p><p>{A}</p>")
    read = {"text": "", "calls": [("r1", "read", json.dumps({"chapters": ["2"], "format": "html"}))]}
    run, provider = _run(book, [read, "好。"], monkeypatch)
    shown = provider.results[-1]["content"]
    token = re.search(r"\{\{IMAGE_PLACEHOLDER_(\d+)\}\}", shown).group(0)
    write = {"text": "", "calls": [("w1", "update_document", json.dumps({"chapter": "2", "html": f"<p>改写。</p><p>{token}</p>"}, ensure_ascii=False))]}
    run, provider = _run(book, [read, write, "好。"], monkeypatch, "改写")
    assert run.status == "done", run.error
    stored = content_of("doc-2")
    assert A in stored and "60px" not in stored
