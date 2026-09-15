"""Backtest run provenance is reproducible without storing source."""

from __future__ import annotations

import subprocess
from pathlib import Path

from trading_bot.backtest.runs import code_revision


def _git(repository: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def test_code_revision_ignores_claude_flow_bookkeeping(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Tests")
    source = tmp_path / "strategy.py"
    source.write_text("EDGE = 1\n")
    _git(tmp_path, "add", "strategy.py")
    _git(tmp_path, "commit", "-m", "initial")

    clean = code_revision(tmp_path)
    scratch = tmp_path / ".claude-flow" / "state.json"
    scratch.parent.mkdir()
    scratch.write_text("{}\n")
    with_scratch = code_revision(tmp_path)

    assert clean == with_scratch


def test_code_revision_hashes_source_like_untracked_files(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Tests")
    (tmp_path / "strategy.py").write_text("EDGE = 1\n")
    _git(tmp_path, "add", "strategy.py")
    _git(tmp_path, "commit", "-m", "initial")

    clean = code_revision(tmp_path)
    (tmp_path / "new_strategy.py").write_text("EDGE = 2\n")
    dirty = code_revision(tmp_path)

    assert clean[1] is False
    assert dirty[1] is True
    assert clean[2] != dirty[2]
