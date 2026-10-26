"""Cache continuity across turns (docs/features/cache_continuity.md): each
turn's request extends the previous one — transcripts replayed, the tail
sent as differences, the ledger frozen between summaries, and the summary
asked for at the end of the live conversation."""
import asyncio
from typing import Any, Dict, List, Optional

import server_context

CONFIG = {"model": "grok-4.6", "agentMaxSteps": 0}


def docs(pinned2: bool = False, two: str = "<p>two</p>", one: str = "<p>alpha</p>") -> List[Dict[str, Any]]:
    return [{"id": "doc-1", "title": "Chapter 1", "content": one},
            {"id": "doc-2", "title": "Chapter 2", "content": two, "pinned": pinned2}]


def assemble(state: Dict[str, Any], *, history: Optional[List[Dict[str, Any]]] = None, documents: Optional[List[Dict[str, Any]]] = None,
             transcripts: Optional[Dict[str, Dict[str, Any]]] = None, turn: str = "", prompt: str = "go", config: Optional[Dict[str, Any]] = None,
             window: Optional[int] = None, stored: Optional[Dict[str, str]] = None, summarize_live=None, summarize=None) -> Dict[str, Any]:
    return asyncio.run(server_context.assemble_request(
        provider="grok", config=config or CONFIG, prompt_text=prompt, images=None, history=history or [],
        documents=documents or docs(), active_document_id="doc-1", selected_text="", custom_instructions=None,
        context_window_tokens=window, state=state, image_registry=[], attachments=[], stored_summary=stored,
        summarize=summarize, transcripts=transcripts, turn=turn, summarize_live=summarize_live))


def transcript_of(request: Dict[str, Any], user_id: str, reply: str = "done") -> Dict[str, Any]:
    """What the run records when its turn ends on a plain reply: its final user message as sent, then the reply."""
    return {"userMessageId": user_id, "messages": [dict(request["apiMessages"][-1]), {"role": "assistant", "content": reply}]}


def pair(n: int, reply: str = "done") -> List[Dict[str, Any]]:
    return [{"id": f"u{n}", "role": "user", "content": f"p{n}"}, {"id": f"a{n}", "role": "assistant", "content": reply}]


def test_the_next_turn_extends_the_previous_request_and_abbreviates_what_did_not_change():
    state = server_context.empty_state()
    first = assemble(state, turn="a1")["apiMessages"]
    assert "CHAPTER INDEX" in first[-1]["content"] and "CURRENT ACTIVE DOCUMENT CONTENT" in first[-1]["content"]
    t = {"a1": transcript_of({"apiMessages": first}, "u1")}
    second = assemble(state, history=pair(1), transcripts=t, turn="a2")["apiMessages"]
    # Byte for byte: the previous request is the start of this one.
    assert second[: len(first)] == first
    assert second[len(first)] == {"role": "assistant", "content": "done", "cacheHint": True}
    tail = second[-1]["content"]
    assert "CHAPTER INDEX: unchanged since your last turn" in tail
    assert 'CURRENT ACTIVE DOCUMENT: #1 "Chapter 1" — unchanged since your last turn' in tail
    assert "alpha" not in tail


def test_a_part_is_sent_in_full_when_its_copy_is_not_replayed_or_changed():
    state = server_context.empty_state()
    first = assemble(state, turn="a1")["apiMessages"]
    # No transcript (a reload, the other transport): the collapsed pair, and everything in full.
    second = assemble(state, history=pair(1), transcripts={}, turn="a2")["apiMessages"]
    assert second[1] == {"role": "user", "content": "p1"}
    assert "CURRENT ACTIVE DOCUMENT CONTENT" in second[-1]["content"] and "alpha" in second[-1]["content"]
    # The active chapter changed: in full, though its earlier copy is replayed.
    t = {"a1": transcript_of({"apiMessages": first}, "u1"), "a2": transcript_of({"apiMessages": second}, "u2")}
    third = assemble(state, history=[*pair(1), *pair(2)], transcripts=t, documents=docs(one="<p>alpha, edited</p>"), turn="a3")["apiMessages"]
    assert "alpha, edited" in third[-1]["content"]


def test_a_transcript_naming_another_user_message_is_not_replayed():
    state = server_context.empty_state()
    first = assemble(state, turn="a1")["apiMessages"]
    t = {"a1": transcript_of({"apiMessages": first}, "someone-else")}
    second = assemble(state, history=pair(1), transcripts=t, turn="a2")["apiMessages"]
    assert second[1] == {"role": "user", "content": "p1"}


def test_the_ledger_stays_frozen_and_an_edited_pin_goes_into_the_tail():
    state = server_context.empty_state()
    first = assemble(state, documents=docs(pinned2=True), turn="a1")["apiMessages"]
    assert "REFERENCED CHAPTERS" in first[1]["content"] and "two" in first[1]["content"]
    t = {"a1": transcript_of({"apiMessages": first}, "u1")}
    edited = docs(pinned2=True, two="<p>two, edited</p>")
    second = assemble(state, history=pair(1), transcripts=t, documents=edited, turn="a2")
    msgs = second["apiMessages"]
    assert msgs[: len(first)] == first  # the ledger did not move
    assert "PINNED CHAPTERS — changed or pinned since the copy you have" in msgs[-1]["content"]
    assert "#2 Chapter 2 (UPDATED" in msgs[-1]["content"] and "two, edited" in msgs[-1]["content"]
    assert "doc-2" in second["inContextIds"]
    # The next turn replays the copy just sent: nothing more to say.
    t["a2"] = transcript_of(second, "u2")
    third = assemble(state, history=[*pair(1), *pair(2)], transcripts=t, documents=edited, turn="a3")["apiMessages"]
    assert "PINNED CHAPTERS" not in third[-1]["content"]
    # Without that transcript, the copy is gone: sent again.
    state_copy = server_context.empty_state()
    assemble(state_copy, documents=docs(pinned2=True), turn="a1")
    assemble(state_copy, history=pair(1), transcripts={"a1": t["a1"]}, documents=edited, turn="a2")
    again = assemble(state_copy, history=[*pair(1), *pair(2)], transcripts={"a1": t["a1"]}, documents=edited, turn="a3")["apiMessages"]
    assert "two, edited" in again[-1]["content"]


def test_an_unpinned_chapter_is_named_once_to_be_disregarded():
    state = server_context.empty_state()
    first = assemble(state, documents=docs(pinned2=True), turn="a1")
    t = {"a1": transcript_of(first, "u1")}
    second = assemble(state, history=pair(1), transcripts=t, documents=docs(pinned2=False), turn="a2")
    assert 'No longer pinned — disregard the earlier copies of: #2 "Chapter 2"' in second["apiMessages"][-1]["content"]
    assert "doc-2" not in second["inContextIds"]
    t["a2"] = transcript_of(second, "u2")
    third = assemble(state, history=[*pair(1), *pair(2)], transcripts=t, documents=docs(pinned2=False), turn="a3")
    assert "No longer pinned" not in third["apiMessages"][-1]["content"]


def test_a_state_from_before_keeps_its_ledger_and_a_new_summary_rebuilds_it():
    state = server_context.empty_state()
    assemble(state, documents=docs(pinned2=True), turn="a1")
    del state["epochSummary"]
    before = list(state["ledger"]["entries"])
    assemble(state, history=pair(1), documents=docs(pinned2=True, two="<p>changed</p>"), turn="a2")
    assert state["ledger"]["entries"] == before and state["epochSummary"] == ""


def test_the_summary_is_asked_for_at_the_end_of_the_live_conversation():
    state = server_context.empty_state()
    config = {**CONFIG, "maxOutputTokens": 16_384}
    history = []
    for i in range(14):
        history += pair(i, reply=f"reply {i} " + "word " * 1100)
    asked: List[List[Dict[str, Any]]] = []

    async def live(messages):
        asked.append(messages)
        return "<summary>\n1. All of it.\n</summary>"
    assemble(state, config=config, window=40000, documents=docs(pinned2=True), turn="a0")  # the ledger the conversation carries
    out = assemble(state, history=history, config=config, window=40000, documents=docs(pinned2=True), summarize_live=live, turn="a14")
    assert len(asked) == 1
    req = asked[0]
    assert req[0] == out["apiMessages"][0]  # the turn's own system prompt
    assert req[-1]["role"] == "user" and req[-1]["content"].startswith("STOP — this is not a request to continue the work.")
    assert 'the user wrote "p' in req[-1]["content"]
    assert req[1]["content"].startswith("REFERENCED CHAPTERS")
    assert out["chatSummary"]["text"] == "1. All of it."
    assert state["epochSummary"] == out["chatSummary"]["upToId"]
    note = next(m for m in out["apiMessages"] if "<conversation_summary>" in (m.get("content") or ""))
    assert "1. All of it." in note["content"]


def test_a_live_prefix_past_the_window_falls_back_to_the_separate_summary():
    state = server_context.empty_state()
    history = []
    for i in range(16):
        history += pair(i, reply="word " * 1200)
    calls = {"live": 0, "plain": 0}

    async def live(messages):
        calls["live"] += 1
        return "<summary>x</summary>"

    async def plain(system, user):
        calls["plain"] += 1
        return "<summary>y</summary>"
    out = assemble(state, history=history, window=8000, summarize_live=live, summarize=plain, turn="a")
    assert calls == {"live": 0, "plain": 1} and out["chatSummary"]["text"] == "y"


def test_transcripts_are_stored_per_reply_pruned_and_deleted_with_the_book(tmp_path, monkeypatch):
    import server_db
    monkeypatch.setattr(server_db, "DB_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setattr(server_context, "MAX_TRANSCRIPTS_PER_BOOK", 2)
    server_db.init_db()
    server_context.ensure_tables()
    for i in range(3):
        server_context.save_transcript("alice", "b1", f"a{i}", f"u{i}", [{"role": "user", "content": f"t{i}"}], f"2026-10-10T00:00:0{i}Z", "grok|g")
    got = server_context.load_transcripts("alice", "b1", ["a0", "a1", "a2", "zz"], "grok|g")
    assert sorted(got) == ["a1", "a2"] and got["a2"] == {"userMessageId": "u2", "messages": [{"role": "user", "content": "t2"}]}
    # Another provider or model gets the collapsed pair: these are grok's ciphertexts.
    assert server_context.load_transcripts("alice", "b1", ["a1", "a2"], "openai|gpt-5.5") == {}
    server_context.delete_book_context("alice", "b1")
    assert server_context.load_transcripts("alice", "b1", ["a1", "a2"], "grok|g") == {}
