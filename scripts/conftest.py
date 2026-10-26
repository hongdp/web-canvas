"""Shared pytest setup for the backend tests."""
import pytest

import server_generation


@pytest.fixture(autouse=True)
def _journal_in_tmp(tmp_path, monkeypatch):
    """Finished test jobs must not land in the real step journal (.local_db)."""
    monkeypatch.setattr(server_generation, "JOURNAL_DIR", str(tmp_path / "step-journal"))
    monkeypatch.setattr(server_generation, "_journal_pruned_on", None)


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    """A transient failure is retried with a backoff (agentic_chat_loop.md §0.10); tests must not sleep through it."""
    monkeypatch.setattr(server_generation, "with_jitter", lambda ms, unit: 0)
