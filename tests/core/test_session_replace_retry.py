"""Windows session-file contention should retry persistence, never the tool."""

import errno
import importlib
import os
import threading

import pytest

from my_agent_core.session import Session


session_module = importlib.import_module("my_agent_core.session.session")


def windows_permission_error(code):
    error = PermissionError(errno.EACCES, "session file is busy")
    error.winerror = code
    return error


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_session_save_recovers_from_temporary_windows_contention(tmp_path, monkeypatch, winerror):
    session = Session(path=tmp_path / "session.jsonl")
    session.add_message("user", "read a file")
    original_replace = session_module.os.replace
    calls = []

    def busy_then_replace(source, target):
        calls.append(source)
        if len(calls) < 3:
            raise windows_permission_error(winerror)
        return original_replace(source, target)

    monkeypatch.setattr(session_module.os, "replace", busy_then_replace)
    session.add_message("tool", "already executed", tool_call_id="call_1")

    assert len(calls) == 3
    assert len(set(calls)) == 1  # Retry the same completed snapshot.
    restored = Session.load(session.path)
    assert [message.content for message in restored.get_full_history_messages()] == ["read a file", "already executed"]
    assert not list(tmp_path.glob("*.tmp"))


def test_persistent_contention_is_bounded_and_preserves_previous_file(tmp_path, monkeypatch):
    session = Session(path=tmp_path / "session.jsonl")
    session.add_message("user", "original")
    original = session.path.read_bytes()
    calls = []

    def always_busy(source, target):
        calls.append(source)
        raise windows_permission_error(5)

    monkeypatch.setattr(session_module.os, "replace", always_busy)
    with pytest.raises(PermissionError):
        session.add_message("assistant", "unsaved")
    assert 1 < len(calls) <= 6
    assert session.path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_non_windows_permission_error_is_not_retried(tmp_path, monkeypatch):
    session = Session(path=tmp_path / "session.jsonl")
    calls = []

    def denied(source, target):
        calls.append(source)
        raise PermissionError(errno.EACCES, "persistent permissions")

    monkeypatch.setattr(session_module.os, "replace", denied)
    with pytest.raises(PermissionError):
        session.save()
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows file-sharing semantics")
def test_save_survives_a_real_windows_reader_that_temporarily_blocks_delete(tmp_path):
    import ctypes
    from ctypes import wintypes

    session = Session(path=tmp_path / "session.jsonl")
    session.add_message("user", "before")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    # Allow other readers, but not deletion/replacement until this handle closes.
    handle = kernel32.CreateFileW(str(session.path), 0x80000000, 0x1, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value
    released = threading.Timer(0.08, kernel32.CloseHandle, args=(handle,))
    released.start()
    try:
        session.add_message("assistant", "after")
    finally:
        released.join()
    assert [message.content for message in Session.load(session.path).get_full_history_messages()] == ["before", "after"]
