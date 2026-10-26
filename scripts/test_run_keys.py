"""Provider API keys at rest (backend_authority.md §4.3): a live run's key is
sealed in the stored request, a finished run keeps none, a restart still
resumes with the key, and rows written before this are scrubbed once."""
import asyncio
import json
import os
import stat
from datetime import datetime, timezone

import server_db
import server_generation
import server_runs
import server_secrets
from test_runs import Scripted, book, request, settle  # noqa: F401 — `book` is a fixture

KEY = "xai-test-secret-0123456789abcdef"


def stored(run_id: str) -> dict:
    conn = server_db.get_db()
    try:
        row = conn.execute("SELECT request FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row["request"])


def keyed_request(prompt: str = "写第一章") -> dict:
    req = request(prompt)
    return {**req, "config": {**req["config"], "apiKey": KEY}}


def test_a_live_run_stores_its_key_sealed_and_a_finished_one_none(book, monkeypatch):  # noqa: F811
    provider = Scripted(["<canvas><p>new</p></canvas>", "done."])
    provider.gate = asyncio.Event()
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", keyed_request())
        await asyncio.sleep(0.01)
        live = stored(run.id)
        provider.gate.set()
        await settle(run)
        return run, live
    run, live = asyncio.run(main())
    assert "apiKey" not in live["config"] and live["config"]["apiKeySealed"]
    assert server_secrets.unseal(live["config"]["apiKeySealed"]) == KEY
    assert run.status == "done", run.error
    done = stored(run.id)
    assert "apiKey" not in done["config"] and "apiKeySealed" not in done["config"]
    assert KEY not in json.dumps(done)
    # The provider was called with the key all along.
    assert all(r["config"]["apiKey"] == KEY for r in provider.requests)


def test_a_restart_resumes_with_the_sealed_key(book, monkeypatch):  # noqa: F811
    provider = Scripted([{"text": "", "calls": [("c1", "read_chapter", '{"chapters":["2"]}')]}, "<canvas><p>after</p></canvas>", "done."], by_step=True)
    monkeypatch.setattr(server_generation, "_dispatch_provider", provider)

    async def main():
        run = book.submit("alice", "book-1", keyed_request())
        while run.record.get("steps", 0) < 1:
            await asyncio.sleep(0.001)
        run.task.cancel()
        try:
            await run.task
        except asyncio.CancelledError:
            pass
        assert KEY not in json.dumps(stored(run.id))
        fresh = server_runs.RunEngine()
        monkeypatch.setattr(server_runs, "engine", fresh)
        await fresh.recover()
        revived = fresh.get("alice", run.id)
        await settle(revived)
        return revived
    revived = asyncio.run(main())
    assert revived.status == "done", revived.error
    assert provider.requests[-1]["config"]["apiKey"] == KEY


def test_a_key_that_cannot_be_opened_stays_missing():
    out = server_runs.restored_request({"config": {"apiKeySealed": "not-a-token", "model": "m"}})
    assert out["config"] == {"model": "m"}


def test_rows_written_before_sealing_are_scrubbed_once(book):  # noqa: F811
    now = datetime.now(timezone.utc).isoformat()
    conn = server_db.get_db()
    try:
        for run_id, status in (("run-old-done", "done"), ("run-old-paused", "paused"), ("run-old-error", "error")):
            conn.execute("INSERT INTO runs (id, username, book_id, status, created_at, updated_at, request) VALUES (?, 'alice', 'book-1', ?, ?, ?, ?)",
                         (run_id, status, now, now, json.dumps(keyed_request(), ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    server_runs.ensure_tables()
    for run_id in ("run-old-done", "run-old-error"):
        cfg = stored(run_id)["config"]
        assert "apiKey" not in cfg and "apiKeySealed" not in cfg
    paused = stored("run-old-paused")
    assert "apiKey" not in paused["config"]
    assert server_runs.restored_request(paused)["config"]["apiKey"] == KEY
    # Vacuumed: the old text is not left in the file's free pages.
    with open(server_db.DB_PATH, "rb") as f:
        assert KEY.encode() not in f.read()
    # Idempotent: a second pass changes nothing.
    server_runs.ensure_tables()
    assert stored("run-old-paused")["config"]["apiKeySealed"] == paused["config"]["apiKeySealed"]


def test_the_key_file_is_private(book):  # noqa: F811
    server_secrets.seal("x")
    mode = stat.S_IMODE(os.stat(server_secrets.key_path()).st_mode)
    assert mode == 0o600
