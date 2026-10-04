"""Project locking that cannot wedge a project after a crash (section 31)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.project.layout import ProjectLayout
from app.project.lock import LockInfo, ProjectLock, process_is_alive


def make_layout(tmp_path: Path) -> ProjectLayout:
    layout = ProjectLayout(tmp_path / "Locked Project")
    layout.ensure()
    return layout


def test_a_lock_is_written_and_released(tmp_path) -> None:
    layout = make_layout(tmp_path)
    lock = ProjectLock(layout)

    result = lock.acquire()
    assert result.ok
    assert layout.lock_file.exists()
    info = json.loads(layout.lock_file.read_text(encoding="utf-8"))
    assert info["pid"] == os.getpid()

    assert lock.release() is True
    assert not layout.lock_file.exists()


def test_a_second_lock_from_a_live_process_is_refused(tmp_path) -> None:
    layout = make_layout(tmp_path)
    first = ProjectLock(layout)
    assert first.acquire().ok

    second = ProjectLock(layout)
    result = second.acquire()

    assert result.ok is False
    assert "already open" in result.reason
    assert result.owner is not None and result.owner.pid == os.getpid()
    assert first.release() is True


def test_a_lock_left_by_a_dead_process_is_removed(tmp_path) -> None:
    """A crash must not permanently block the project."""
    layout = make_layout(tmp_path)
    layout.lock_file.write_text(
        json.dumps(
            {
                "pid": 999999,  # not running
                "host": __import__("socket").gethostname(),
                "started_at": "2026-01-01 00:00:00",
                "app_version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    result = ProjectLock(layout).acquire()

    assert result.ok is True
    assert result.stale_removed is True
    assert json.loads(layout.lock_file.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_a_lock_from_another_computer_is_never_removed_automatically(tmp_path) -> None:
    layout = make_layout(tmp_path)
    layout.lock_file.write_text(
        json.dumps({"pid": 4242, "host": "some-other-pc", "started_at": "2026-01-01 00:00:00"}),
        encoding="utf-8",
    )

    result = ProjectLock(layout).acquire()

    assert result.ok is False
    assert "another computer" in result.reason or "some-other-pc" in result.reason
    assert layout.lock_file.exists(), "a lock this machine cannot verify stays untouched"

    forced = ProjectLock(layout).acquire(force=True)
    assert forced.ok is True


def test_an_unreadable_lock_is_treated_as_stale(tmp_path) -> None:
    layout = make_layout(tmp_path)
    layout.lock_file.write_text("{ not json", encoding="utf-8")

    result = ProjectLock(layout).acquire()

    assert result.ok is True
    assert result.stale_removed is True


def test_releasing_only_removes_our_own_lock(tmp_path) -> None:
    layout = make_layout(tmp_path)
    layout.lock_file.write_text(json.dumps({"pid": os.getpid() + 12345, "host": ""}), encoding="utf-8")

    assert ProjectLock(layout).release() is False
    assert layout.lock_file.exists()


def test_a_lock_failure_never_blocks_opening(tmp_path, monkeypatch) -> None:
    layout = make_layout(tmp_path)

    def deny(*_args, **_kwargs):
        raise OSError("read-only folder")

    from app.project import lock as lock_module

    monkeypatch.setattr(lock_module, "atomic_write_json", deny)

    result = ProjectLock(layout).acquire()

    assert result.ok is True
    assert result.advisory_only is True
    assert "could not be written" in result.reason


def test_the_lock_describes_its_owner(tmp_path) -> None:
    layout = make_layout(tmp_path)
    lock = ProjectLock(layout)
    lock.acquire()

    info = lock.owner()
    assert isinstance(info, LockInfo)
    assert "this computer" in info.describe()
    assert info.is_local is True
    lock.release()


def test_process_probe_handles_impossible_pids() -> None:
    assert process_is_alive(0) is False
    assert process_is_alive(-5) is False
    assert process_is_alive(os.getpid()) is True
    assert process_is_alive(4_000_000) is False
