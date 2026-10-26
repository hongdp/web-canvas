"""The step journal (server_generation.journal_step).

A run that went wrong must be explainable afterwards: each finished step
keeps its reasoning, its visible text and its tool calls.
"""
import json
import os
import time

import server_generation as gen


def _job(**kw):
    job = gen.GenerationJob("gen-test", "alice", {"kind": "chat", "assistantMessageId": "a-1"})
    job.conversation = "book-1"
    job.model = "grok-4.7"
    for k, v in kw.items():
        setattr(job, k, v)
    return job


def _lines(folder):
    files = [f for f in os.listdir(folder) if f.endswith(".jsonl")]
    assert len(files) == 1
    with open(os.path.join(folder, files[0]), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def test_a_finished_step_is_journaled_with_its_reasoning_text_and_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "_journal_pruned_on", None)
    job = _job()
    job.note_reasoning("先把设定写成一章。")
    job.append("设定另起一章。")
    job.tool_calls[0] = {"id": "c1", "name": "list_chapters", "arguments": "{}"}
    job.finish("done")
    [record] = _lines(tmp_path)
    assert record["reasoning"] == "先把设定写成一章。"
    assert record["text"] == "设定另起一章。"
    assert record["toolCalls"] == [{"name": "list_chapters", "arguments": "{}"}]
    assert record["conversation"] == "book-1" and record["message"] == "a-1" and record["status"] == "done"


def test_old_journal_files_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "_journal_pruned_on", None)
    old = tmp_path / "2020-01-01.jsonl"
    old.write_text("{}\n")
    stamp = time.time() - (gen.JOURNAL_KEEP_DAYS + 1) * 86400
    os.utime(old, (stamp, stamp))
    _job().finish("done")
    assert not old.exists()


def test_a_journal_that_cannot_be_written_never_fails_the_job(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(gen, "JOURNAL_DIR", str(blocker / "sub"))
    job = _job()
    assert job.finish("done") is True
    assert job.status == "done"
