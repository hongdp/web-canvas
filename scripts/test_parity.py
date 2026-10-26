"""TypeScript ↔ Python parity (backend_authority.md phase 2).

scripts/parity/fixtures/*.json are written by src/parity/__tests__/parity.test.ts
from the TypeScript implementation. Each case here must reproduce its output
byte for byte. A failure means the port differs from the specification.

PYTHON_OWNED fixtures are the exception (phase 4, 2026-10-17): their
TypeScript went with the browser's agent loop, so the Python is the
specification and the files are golden. After an intended change to one of
those modules, rewrite their outputs with
    python3 scripts/test_parity.py --rewrite-python-owned
and review the diff.
"""
import json
import os
import re

import pytest

from wc_text import (attachments, web_text, chapter_index, chapters, context_ledger, context_selection, context_window, conversation_summary, diff, diff_resolution,
                     document_tools, dynamic_context, edit_hints, freshness, image_preservation, invocations, llm_context, paragraphs, pending_changes,
                     plan, policy, polish, protocol_choice, provider_profile, reminders, retry_policy, run_compaction, stream_handlers, system_prompt, text,
                     title_sync, tool_call_stream, turn_transcripts, reasoning_effort, text_replace)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parity", "fixtures")
#: Fixtures with no TypeScript left to write them (src/parity PYTHON_OWNED).
PYTHON_OWNED = ("freshness", "policy", "chapters", "invocations", "reminders", "run_compaction",
                # 2026-10-18: the TypeScript of these went too (nothing in the frontend ran it).
                "llm_context", "pending_changes", "image_preservation", "polish", "chapter_index", "dynamic_context",
                "context_selection", "context_window", "provider_profile", "tool_call_stream", "edit_hints", "plan",
                "conversation_summary", "turn_transcripts", "text_replace", "web_text", "retry_policy", "system_prompt",
                "document_tools", "attachments")


def normalize_diff_ids(html: str) -> str:
    seen = {}

    def sub(m):
        key = m.group(1)
        if key not in seen:
            seen[key] = f"diff-{len(seen) + 1}"
        return f'data-diff-id="{seen[key]}"'
    return re.sub(r'data-diff-id="([^"]*)"', sub, html)


def _replace_images(html, registry):
    out = image_preservation.replace_images_with_placeholders(html, registry)
    return {"html": out, "registry": registry}


def _plan_ledger(current, desired, docs, active, o=None):
    o = o or {}
    options = {}
    if o.get("render"):
        options["render"] = lambda doc_id, kind: f"[{kind}:{doc_id}]"
    if o.get("maxStaleChars") is not None:
        options["maxStaleChars"] = o["maxStaleChars"]
    if o.get("keepIds"):
        options["keepIds"] = o["keepIds"]
    return context_ledger.plan_ledger_turn(current, desired, docs, active, options)


def _volatile_tail(docs, active, selected, opts=None):
    registry = []
    return dynamic_context.build_volatile_tail(docs, active, selected, lambda h: image_preservation.replace_images_with_placeholders(h, registry), opts)


def _freshness(docs, active, in_context, seen, turn):
    record = {k: dict(v) for k, v in seen.items()}
    markers = freshness.freshness_markers(docs, active, in_context, record, turn)
    return {"markers": markers, "seen": record}


def _accumulate(accumulators, delta):
    """applyToolCallDelta, kept here only to build finish_tool_calls' input: the server
    accumulates deltas itself (GenerationJob.note_tool_call), so it has no port of it."""
    index = delta.get("index") if isinstance(delta.get("index"), int) and not isinstance(delta.get("index"), bool) else 0
    existing = accumulators.get(index) or {"argumentsText": ""}
    if delta.get("id"):
        existing["id"] = delta["id"]
    fn = delta.get("function") or {}
    if fn.get("name"):
        existing["name"] = fn["name"]
    if delta.get("signature"):
        existing["signature"] = delta["signature"]
    if fn.get("arguments") is not None:
        existing["argumentsText"] = fn["arguments"] if delta.get("replace") else existing["argumentsText"] + fn["arguments"]
    accumulators[index] = existing


def _apply_deltas(deltas):
    acc = {}
    for d in deltas:
        _accumulate(acc, d)
    return {"accumulators": {str(k): v for k, v in acc.items()}, "finished": tool_call_stream.finish_tool_calls(acc)}


_TOOL_DESCRIPTORS = {
    "update_document": {"kind": "write", "markupForm": True}, "edit_document": {"kind": "write", "markupForm": True},
    "replace_selection": {"kind": "write", "markupForm": True}, "polish_chapter": {"kind": "write"},
    "read_chapter": {"kind": "read"}, "open_chapter": {"kind": "navigate"},
}


def _collect(text, native_calls, step, opts=None):
    return invocations.collect_step(text, native_calls, _TOOL_DESCRIPTORS.get, step, opts)


def _decide(executed, steps_taken, corrective_used, budgets, pol):
    return policy.decide_after_step({"executed": executed, "stepsTaken": steps_taken, "correctiveUsed": corrective_used, "budgets": budgets, "policy": pol})


def _apply_locally(html, blocks):
    out = text.apply_edit_blocks_locally(html, blocks)
    return {**out, "html": normalize_diff_ids(out["html"])}


FUNCTIONS = {
    ("text", "strip_incomplete_end_tag"): text.strip_incomplete_end_tag,
    ("text", "chapter_attribute"): text.chapter_attribute,
    ("text", "new_chapter_attribute"): text.new_chapter_attribute,
    ("text", "extract_tagged_block"): text.extract_tagged_block,
    ("text", "has_elision_markers"): text.has_elision_markers,
    ("text", "validate_canvas_replacement"): text.validate_canvas_replacement,
    ("text", "parse_edit_blocks"): text.parse_edit_blocks,
    ("text", "strip_stray_document_markup"): text.strip_stray_document_markup,
    ("text", "parse_assistant_response"): text.parse_assistant_response,
    ("text", "apply_edit_blocks"): text.apply_edit_blocks,
    ("text", "apply_edit_blocks_locally"): _apply_locally,
    ("text", "strip_blank_paragraphs"): text.strip_blank_paragraphs,
    ("text", "parse_doc_status"): text.parse_doc_status,
    ("text", "strip_doc_status"): text.strip_doc_status,
    ("text", "detect_failed_document_update"): text.detect_failed_document_update,
    ("text", "claims_own_write"): text.claims_own_write,
    ("text", "trim_incomplete_html_tail"): text.trim_incomplete_html_tail,
    ("text", "is_blank_content"): text.is_blank_content,
    ("llm_context", "html_to_plain_text"): llm_context.html_to_plain_text,
    ("llm_context", "strip_chat_display_artifacts"): llm_context.strip_chat_display_artifacts,
    ("llm_context", "truncate_with_notice"): llm_context.truncate_with_notice,
    ("llm_context", "detect_referenced_doc_ids"): llm_context.detect_referenced_doc_ids,
    ("llm_context", "build_attachments_label"): llm_context.build_attachments_label,
    ("llm_context", "trim_history_for_context"): llm_context.trim_history_for_context,
    ("llm_context", "was_turn_interrupted"): llm_context.was_turn_interrupted,
    ("polish", "bare"): polish.bare,
    ("polish", "split_for_polish"): polish.split_for_polish,
    ("polish", "build_polish_prompt"): polish.build_polish_prompt,
    ("polish", "parse_polished"): polish.parse_polished,
    ("polish", "validate_polished"): polish.validate_polished,
    ("polish", "assemble_polished"): polish.assemble_polished,
    ("paragraphs", "rewrite_loss"): paragraphs.rewrite_loss,
    ("paragraphs", "rewrite_loss_note"): paragraphs.rewrite_loss_note,
    ("paragraphs", "block_text"): paragraphs.block_text,
    ("paragraphs", "top_level_blocks"): paragraphs.top_level_blocks,
    ("paragraphs", "chapter_paragraphs"): paragraphs.chapter_paragraphs,
    ("paragraphs", "chapter_chars"): paragraphs.chapter_chars,
    ("paragraphs", "numbered_line"): paragraphs.numbered_line,
    ("paragraphs", "searchable_text"): paragraphs.searchable_text,
    ("paragraphs", "block_images"): paragraphs.block_images,
    ("paragraphs", "paragraph_spans"): paragraphs.paragraph_spans,
    ("paragraphs", "numbered_paragraph_spans"): paragraphs.numbered_paragraph_spans,
    ("paragraphs", "is_plain_chapter_html"): paragraphs.is_plain_chapter_html,
    ("paragraphs", "is_text_rewritable_html"): paragraphs.is_text_rewritable_html,
    ("paragraphs", "strip_image_marker_paragraphs"): paragraphs.strip_image_marker_paragraphs,
    ("paragraphs", "as_blocks"): paragraphs.as_blocks,
    ("paragraphs", "apply_paragraph_edits"): paragraphs.apply_paragraph_edits,
    ("paragraphs", "paragraph_blocks"): paragraphs.paragraph_blocks,
    ("paragraphs", "anchor_matches"): paragraphs.anchor_matches,
    ("paragraphs", "as_items"): paragraphs.as_items,
    ("paragraphs", "paragraph_spans_card"): paragraphs.paragraph_spans,
    ("paragraphs", "numbered_paragraph_spans_card"): paragraphs.numbered_paragraph_spans,
    ("diff", "diff_html"): lambda a, b: normalize_diff_ids(diff.diff_html(a, b)),
    ("diff", "strip_diff_markup"): diff.strip_diff_markup,
    ("diff_resolution", "resolve_diff_markup_in_html"): diff_resolution.resolve_diff_markup_in_html,
    ("pending_changes", "pending_changes"): pending_changes.pending_changes,
    ("pending_changes", "render_pending_changes"): pending_changes.render_pending_changes,
    ("image_preservation", "replace_images_with_placeholders"): _replace_images,
    ("image_preservation", "restore_image_placeholders"): image_preservation.restore_image_placeholders,
    ("image_preservation", "register_image"): lambda registry, data_url, alt: {"placeholder": image_preservation.register_image(registry, data_url, alt), "registry": registry},
    ("image_preservation", "reinsert_missing_images"): image_preservation.reinsert_missing_images,
    ("chapter_index", "build_chapter_index"): chapter_index.build_chapter_index,
    ("chapter_index", "pack_chapters_into_batches"): chapter_index.pack_chapters_into_batches,
    ("chapter_index", "analyze_batch_tokens"): lambda: chapter_index.ANALYZE_BATCH_TOKENS,
    ("chapter_index", "analyze_batch_chars"): chapter_index.analyze_batch_chars,
    ("dynamic_context", "render_ledger_chapter"): dynamic_context.render_ledger_chapter,
    ("dynamic_context", "ledger_block"): dynamic_context.ledger_block,
    ("dynamic_context", "build_ledger_messages"): dynamic_context.build_ledger_messages,
    ("dynamic_context", "build_volatile_tail"): _volatile_tail,
    ("dynamic_context", "diff_tail_parts"): dynamic_context.diff_tail_parts,
    ("dynamic_context", "pinned_updates"): dynamic_context.pinned_updates,
    ("context_ledger", "hash_content"): context_ledger.hash_content,
    ("context_ledger", "plan_ledger_turn"): _plan_ledger,
    ("context_ledger", "ledger_chapter_ids"): context_ledger.ledger_chapter_ids,
    ("context_ledger", "order_admissions_by_stability"): context_ledger.order_admissions_by_stability,
    ("context_selection", "extract_keywords"): context_selection.extract_keywords,
    ("context_selection", "select_reference_chapters"): context_selection.select_reference_chapters,
    ("context_selection", "pinned_context_ids"): context_selection.pinned_context_ids,
    ("context_selection", "pinned_budget"): lambda: context_selection.PINNED_CONTEXT_CHARS,
    ("system_prompt", "build_chat_system_prompt"): system_prompt.build_chat_system_prompt,
    ("edit_hints", "text_similarity"): edit_hints.text_similarity,
    ("edit_hints", "describe_differences"): edit_hints.describe_differences,
    ("edit_hints", "nearest_paragraph"): edit_hints.nearest_paragraph,
    ("edit_hints", "nearest_hint"): edit_hints.nearest_hint,
    ("plan", "apply_plan_update"): plan.apply_plan_update,
    ("plan", "previous_turn_plan"): plan.previous_turn_plan,
    ("plan", "render_plan"): plan.render_plan,
    ("plan", "next_plan_item"): plan.next_plan_item,
    ("plan", "unfinished_plan_items"): plan.unfinished_plan_items,
    ("reminders", "constants"): lambda: {"REMINDERS_ARE_CONTEXT": reminders.REMINDERS_ARE_CONTEXT, "REPEAT_NUDGE_STEPS": reminders.REPEAT_NUDGE_STEPS,
                                        "REPEAT_PAUSE_STEPS": reminders.REPEAT_PAUSE_STEPS, "PLAN_NUDGE_BUDGET": reminders.PLAN_NUDGE_BUDGET},
    ("conversation_summary", "constants"): lambda: {
        "SUMMARY_SYSTEM_PROMPT": conversation_summary.SUMMARY_SYSTEM_PROMPT, "KEEP_FRACTION": conversation_summary.KEEP_FRACTION,
        "SUMMARY_RESERVE_CHARS": conversation_summary.SUMMARY_RESERVE_CHARS, "SUMMARY_INPUT_CHARS": conversation_summary.SUMMARY_INPUT_CHARS,
        "SUMMARY_MESSAGE_CHARS": conversation_summary.SUMMARY_MESSAGE_CHARS, "SUMMARY_MIN_KEEP": conversation_summary.SUMMARY_MIN_KEEP},
    ("conversation_summary", "plan_conversation_summary"): conversation_summary.plan_conversation_summary,
    ("conversation_summary", "build_summary_request"): conversation_summary.build_summary_request,
    ("conversation_summary", "parse_summary_reply"): conversation_summary.parse_summary_reply,
    ("conversation_summary", "summary_messages"): conversation_summary.summary_messages,
    ("conversation_summary", "build_summary_instruction"): conversation_summary.build_summary_instruction,
    ("conversation_summary", "plan_conversation_summary_weighted"): conversation_summary.plan_conversation_summary,
    ("reasoning_effort", "supported_reasoning_efforts"): reasoning_effort.supported_reasoning_efforts,
    ("reasoning_effort", "resolve_reasoning_effort"): reasoning_effort.resolve_reasoning_effort,
    ("reasoning_effort", "reasoning_budget_tokens"): reasoning_effort.reasoning_budget_tokens,
    ("reasoning_effort", "anthropic_thinking"): reasoning_effort.anthropic_thinking,
    ("reasoning_effort", "gemini_thinking"): reasoning_effort.gemini_thinking,
    ("reasoning_effort", "moonshot_thinking"): reasoning_effort.moonshot_thinking,
    ("turn_transcripts", "plan_history_units"): turn_transcripts.plan_history_units,
    ("turn_transcripts", "transcript_weight"): turn_transcripts.transcript_weight,
    ("turn_transcripts", "summarizable_history"): turn_transcripts.summarizable_history,
    ("turn_transcripts", "history_window"): turn_transcripts.history_window,
    ("run_compaction", "constants"): lambda: {"ELIDE_ABOVE": run_compaction.ELIDE_ABOVE, "ELIDE_TO": run_compaction.ELIDE_TO},
    ("run_compaction", "elided_result_note"): run_compaction.elided_result_note,
    ("run_compaction", "elision_trace"): run_compaction.elision_trace,
    ("run_compaction", "prompt_tokens"): run_compaction.prompt_tokens,
    ("run_compaction", "plan_elisions"): run_compaction.plan_elisions,
    ("text_replace", "escape_html_text"): text_replace.escape_html_text,
    ("text_replace", "expand_replacement"): text_replace.expand_replacement,
    ("text_replace", "replace_in_html_text"): text_replace.replace_in_html_text,
    ("run_compaction", "calibrated_prompt_tokens"): run_compaction.calibrated_prompt_tokens,
    ("attachments", "constants"): lambda: {"ATTACHMENT_INDEX_LINES": attachments.ATTACHMENT_INDEX_LINES, "ATTACHMENT_RUN_READ_CAP": attachments.ATTACHMENT_RUN_READ_CAP},
    ("attachments", "attachment_budget_note"): attachments.attachment_budget_note,
    ("attachments", "parse_chapter_number"): attachments.parse_chapter_number,
    ("attachments", "section_number_of"): attachments.section_number_of,
    ("attachments", "find_attachment_section"): attachments.find_attachment_section,
    ("attachments", "find_attachment_range"): attachments.find_attachment_range,
    ("attachments", "render_section_list"): attachments.render_section_list,
    ("attachments", "list_section_lines"): lambda: attachments.LIST_SECTION_LINES,
    ("attachments", "normalize_attachment_text"): attachments.normalize_attachment_text,
    ("attachments", "attachment_paragraphs"): attachments.attachment_paragraphs,
    ("attachments", "attachment_paragraphs_long"): attachments.attachment_paragraphs,
    ("attachments", "split_long_paragraph"): attachments.split_long_paragraph,
    ("attachments", "attachment_sections"): attachments.attachment_sections,
    ("attachments", "resolve_attachment_ref"): attachments.resolve_attachment_ref,
    ("attachments", "render_attachment_index"): attachments.render_attachment_index,
    ("attachments", "attachment_chunks"): attachments.attachment_chunks,
    ("attachments", "render_attachment_part"): attachments.render_attachment_part,
    ("web_text", "constants"): lambda: {"UNTRUSTED_WEB_NOTE": web_text.UNTRUSTED_WEB_NOTE, "WEB_READ_CAP": web_text.WEB_READ_CAP},
    ("web_text", "render_search_results"): web_text.render_search_results,
    ("web_text", "render_web_page"): web_text.render_web_page,
    ("web_text", "render_web_images"): web_text.render_web_images,
    ("retry_policy", "constants"): lambda: {"MAX_TRANSPORT_RETRIES": retry_policy.MAX_TRANSPORT_RETRIES, "MAX_RETRY_DELAY_MS": retry_policy.MAX_RETRY_DELAY_MS,
                                            "RETRYABLE_STATUSES": retry_policy.RETRYABLE_STATUSES},
    ("retry_policy", "is_retryable_status"): retry_policy.is_retryable_status,
    ("retry_policy", "is_context_length_error"): retry_policy.is_context_length_error,
    ("retry_policy", "parse_retry_after"): retry_policy.parse_retry_after,
    ("retry_policy", "retry_delay_ms"): retry_policy.retry_delay_ms,
    ("retry_policy", "with_jitter"): retry_policy.with_jitter,
    ("reminders", "wrap_reminder"): reminders.wrap_reminder,
    ("reminders", "escape_reminder_tags"): reminders.escape_reminder_tags,
    ("reminders", "interrupted_turn_reminder"): reminders.interrupted_turn_reminder,
    ("reminders", "steer_message"): reminders.steer_message,
    ("reminders", "unbacked_claim_nudge"): reminders.unbacked_claim_nudge,
    ("reminders", "lookup_streak_nudge"): reminders.lookup_streak_nudge,
    ("reminders", "lookup_nudge_steps"): lambda: reminders.LOOKUP_NUDGE_STEPS,
    ("reminders", "append_reminders"): reminders.append_reminders,
    ("reminders", "repeat_nudge"): reminders.repeat_nudge,
    ("reminders", "long_reasoning_reminder"): reminders.long_reasoning_reminder,
    ("reminders", "plan_unfinished_nudge"): reminders.plan_unfinished_nudge,
    ("reminders", "carried_plan_reminder"): reminders.carried_plan_reminder,
    ("reminders", "user_edited_reminder"): reminders.user_edited_reminder,
    ("reminders", "structure_changed_reminder"): reminders.structure_changed_reminder,
    ("reminders", "queued_request_reminder"): reminders.queued_request_reminder,
    ("reminders", "plan_not_written_note"): reminders.plan_not_written_note,
    ("reminders", "html_read_nudge"): reminders.html_read_nudge,
    ("reminders", "call_signature"): tool_call_stream.call_signature,
    ("freshness", "accepted_hash"): freshness.accepted_hash,
    ("freshness", "freshness_markers"): _freshness,
    ("context_window", "resolve_context_window_tokens"): context_window.resolve_context_window_tokens,
    ("context_window", "estimate_tokens"): context_window.estimate_tokens,
    ("context_window", "tokens_to_chars"): context_window.tokens_to_chars,
    ("context_window", "history_budget_chars"): context_window.history_budget_chars,
    ("context_window", "cjk_ratio_of"): context_window.cjk_ratio_of,
    ("provider_profile", "get_cache_profile"): provider_profile.get_cache_profile,
    ("provider_profile", "target_prompt_tokens"): provider_profile.target_prompt_tokens,
    ("provider_profile", "uses_responses_api"): provider_profile.uses_responses_api,
    ("protocol_choice", "resolve_document_protocol"): protocol_choice.resolve_document_protocol,
    ("title_sync", "leading_h1_text"): title_sync.leading_h1_text,
    ("title_sync", "content_with_renamed_heading"): title_sync.content_with_renamed_heading,
    ("tool_call_stream", "partial_string_argument"): tool_call_stream.partial_string_argument,
    ("tool_call_stream", "apply_tool_call_delta"): _apply_deltas,
    ("stream_handlers", "constants"): lambda: {
        "NO_ACTION_RETRY_INSTRUCTION": stream_handlers.NO_ACTION_RETRY_INSTRUCTION, "MAX_NO_ACTION_RETRIES": stream_handlers.MAX_NO_ACTION_RETRIES,
        "ASSISTANT_PLACEHOLDER": stream_handlers.ASSISTANT_PLACEHOLDER, "INTERRUPTED_NOTICE": stream_handlers.INTERRUPTED_NOTICE,
        "RECONNECT_FAILED_NOTICE": stream_handlers.RECONNECT_FAILED_NOTICE, "STEP_LIMIT_NOTE": stream_handlers.STEP_LIMIT_NOTE},
    ("stream_handlers", "split_streaming_response"): stream_handlers.split_streaming_response,
    ("stream_handlers", "build_completion_warnings"): stream_handlers.build_completion_warnings,
    ("policy", "default_max_steps"): policy.default_max_steps,
    ("policy", "resolve_run_settings"): policy.resolve_run_settings,
    ("policy", "detect_step_failure"): policy.detect_step_failure,
    ("policy", "decide_after_step"): _decide,
    ("chapters", "cite_chapter"): chapters.cite_chapter,
    ("chapters", "resolve_chapter"): chapters.resolve_chapter,
    ("invocations", "plan_done_attributes"): invocations.plan_done_attributes,
    ("invocations", "collect_step"): _collect,
    ("invocations", "plan_writes"): invocations.plan_writes,
    ("document_tools", "document_tools"): lambda: document_tools.DOCUMENT_TOOLS,
    ("document_tools", "to_openai_tools"): document_tools.to_openai_tools,
    ("document_tools", "to_anthropic_tools"): document_tools.to_anthropic_tools,
    ("document_tools", "to_gemini_tools"): document_tools.to_gemini_tools,
    ("document_tools", "from_openai_tools"): document_tools.from_openai_tools,
}


def _cases():
    for name in sorted(os.listdir(FIXTURES)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            fixture = json.load(fh)
        for fn, cases in fixture["cases"].items():
            for i, case in enumerate(cases):
                yield pytest.param(fixture["module"], fn, case, id=f"{fixture['module']}.{fn}[{i}]")


@pytest.mark.parametrize("module,fn,case", list(_cases()))
def test_python_matches_typescript(module, fn, case):
    impl = FUNCTIONS[(module, fn)]
    assert impl(*case["input"]) == case["output"]


def test_every_fixture_function_has_a_port():
    missing = []
    for name in os.listdir(FIXTURES):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            fixture = json.load(fh)
        for fn in fixture["cases"]:
            if (fixture["module"], fn) not in FUNCTIONS:
                missing.append(f"{fixture['module']}.{fn}")
    assert missing == []


def _js_numbers(value):
    """Integral floats as integers, the way JSON.stringify wrote the fixtures (1, not 1.0)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_js_numbers(v) for v in value]
    if isinstance(value, dict):
        return {k: _js_numbers(v) for k, v in value.items()}
    return value


def rewrite_python_owned() -> None:
    """Recompute every PYTHON_OWNED case's output from the Python, inputs unchanged."""
    for module in PYTHON_OWNED:
        path = os.path.join(FIXTURES, f"{module}.json")
        with open(path, encoding="utf-8") as fh:
            fixture = json.load(fh)
        for fn, cases in fixture["cases"].items():
            for case in cases:
                output = _js_numbers(json.loads(json.dumps(FUNCTIONS[(module, fn)](*case["input"]))))
                # Only a changed result is rewritten: an equal one keeps its spelling (key order), so the diff shows the change alone.
                if output != case["output"]:
                    case["output"] = output
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(fixture, ensure_ascii=False, indent=1) + "\n")
        print(f"rewrote {path}")


if __name__ == "__main__":
    import sys
    if "--rewrite-python-owned" in sys.argv:
        rewrite_python_owned()
    else:
        print("usage: python3 scripts/test_parity.py --rewrite-python-owned")
