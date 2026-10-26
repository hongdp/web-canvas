"""The prefix line in the generation log (server_generation.describe_prefix).

A turn that lost its cached prefix must say whether the request changed or
the provider missed: the line names the first message that differs.
"""
import server_generation as gen
import wc_providers.common


def _body(*contents, tools=None, model="grok-4.6"):
    return {"model": model, "tools": tools or [], "messages": [{"role": "user", "content": c} for c in contents]}


def setup_function(_):
    wc_providers.common._last_requests.clear()


def test_the_first_request_of_a_conversation_says_so():
    assert gen.describe_prefix("grok:b", _body("a"), now=0) == "first request seen in this conversation"


def test_a_step_that_appends_extends_the_previous_request():
    gen.describe_prefix("grok:b", _body("sys", "q"), now=0)
    assert gen.describe_prefix("grok:b", _body("sys", "q", "more"), now=12) == \
        "12s after the previous request; extends it (all 2 earlier messages identical)"


def test_a_changed_message_is_named_with_its_position():
    gen.describe_prefix("grok:b", _body("sys v1", "q"), now=0)
    line = gen.describe_prefix("grok:b", _body("sys v2", "q"), now=5)
    assert "first 0 of its 2 messages identical; message #0 (user, 6 chars) differs" in line


def test_model_and_tools_changes_are_reported():
    gen.describe_prefix("grok:b", _body("a", tools=[{"name": "x"}]), now=0)
    line = gen.describe_prefix("grok:b", _body("a", tools=[{"name": "y"}], model="grok-5"), now=1)
    assert "model changed (grok-4.6 -> grok-5)" in line
    assert "tools changed" in line


def test_conversations_are_compared_only_with_themselves():
    gen.describe_prefix("grok:book-1", _body("a"), now=0)
    assert gen.describe_prefix("grok:book-2", _body("b"), now=1) == "first request seen in this conversation"


def test_memory_is_bounded():
    for i in range(wc_providers.common.PREFIX_MEMORY + 5):
        gen.describe_prefix(f"grok:{i}", _body("a"), now=i)
    assert len(wc_providers.common._last_requests) == wc_providers.common.PREFIX_MEMORY


def _gemini(*texts, system="sys"):
    return {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": t}]} for t in texts]}


def test_a_gemini_request_is_compared_by_its_system_instruction_and_contents():
    gen.describe_prefix("gemini:b", _gemini("q"), now=0)
    assert gen.describe_prefix("gemini:b", _gemini("q", "more"), now=3) == \
        "3s after the previous request; extends it (all 2 earlier messages identical)"
    line = gen.describe_prefix("gemini:b", _gemini("q", "more", system="sys v2"), now=4)
    assert "first 0 of its 3 messages identical; message #0 (system, 6 chars) differs" in line
