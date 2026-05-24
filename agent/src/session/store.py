"""Filesystem-backed persistence for Session, Message, and Attempt records."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.session.models import Attempt, AttemptStatus, Message, Session

# Architectural gap #4 (Codex audit): atomic write pattern ported from
# SwarmStore (src/swarm/store.py). Plain path.write_text leaves a window
# where a reader sees a partially-written file, and concurrent writers from
# MCP + API server can interleave. .tmp + os.replace gives POSIX atomic
# rename; on Windows replace can race with a reader holding target open
# (WinError 5/32) so we retry with backoff. POSIX path runs once.

_TRANSIENT_WINERRORS = (5, 32)  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION
_REPLACE_ATTEMPTS = 6
_REPLACE_BACKOFF = (0.025, 0.05, 0.1, 0.2, 0.4)  # len == attempts - 1


def _is_transient_windows_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS


def _replace_with_retry(tmp: Path, target: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            if not _is_transient_windows_error(exc):
                raise
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF[attempt])


# Module-level lock — protects all SessionStore instances in this process
# against concurrent _write_json calls (MCP server + API server share the
# store). Cross-process safety still requires file-level locking; this is
# necessary but not sufficient. The TODO is documented in the class
# docstring rather than papered over.
_WRITE_LOCK = threading.Lock()


class SessionStore:
    """Filesystem-backed persistent storage.

    Directory structure::

        sessions/
        ├── {session_id}/
        │   ├── session.json
        │   ├── messages.jsonl
        │   └── attempts/
        │       └── {attempt_id}/
        │           └── attempt.json

    Atomicity (architectural gap #4 from Codex audit):
        - ``_write_json`` writes session.json / attempt.json via
          tmp-file + ``os.replace`` under a module-level lock, so readers
          never observe a partial file and intra-process writers do not
          interleave bytes.
        - ``append_message`` (jsonl) is also lock-guarded; one full JSON
          line per write, with explicit fsync to flush before lock release.
        - Cross-process safety still relies on the OS rename guarantees;
          if multiple worker processes share the same base_dir, add file
          locks (fcntl/portalocker) per session.

    Attributes:
        base_dir: Root directory for session storage.
    """

    def __init__(self, base_dir: Path) -> None:
        """Initialize session storage.

        Args:
            base_dir: Root directory for session storage.
        """
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        return self.base_dir / session_id

    def _session_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "session.json"

    def _messages_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "messages.jsonl"

    def _attempt_dir(self, session_id: str, attempt_id: str) -> Path:
        return self._session_dir(session_id) / "attempts" / attempt_id

    def _attempt_file(self, session_id: str, attempt_id: str) -> Path:
        return self._attempt_dir(session_id, attempt_id) / "attempt.json"

    # ---- Session CRUD ----

    def create_session(self, session: Session) -> Session:
        """Create and persist a session.

        Args:
            session: Session instance to create.

        Returns:
            The persisted Session.

        Raises:
            ValueError: Raised when the session already exists.
        """
        session_dir = self._session_dir(session.session_id)
        if session_dir.exists():
            raise ValueError(f"Session {session.session_id} already exists")
        session_dir.mkdir(parents=True)
        (session_dir / "attempts").mkdir()
        self._write_json(self._session_file(session.session_id), session.to_dict())
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Read a session.

        Args:
            session_id: Session ID.

        Returns:
            The Session instance, or None when it does not exist.
        """
        path = self._session_file(session_id)
        data = self._read_json(path)
        if data is None:
            return None
        return Session.from_dict(data)

    def update_session(self, session: Session) -> None:
        """Update a session.

        Args:
            session: Modified Session instance.
        """
        self._write_json(self._session_file(session.session_id), session.to_dict())

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all of its data.

        Args:
            session_id: Session ID.

        Returns:
            Whether the delete succeeded.
        """
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return False
        import shutil
        shutil.rmtree(session_dir, ignore_errors=True)
        return True

    def list_sessions(self, limit: int = 50) -> List[Session]:
        """List all sessions in descending update-time order.

        Args:
            limit: Maximum number of sessions to return.

        Returns:
            List of Session objects.
        """
        sessions: List[Session] = []
        if not self.base_dir.exists():
            return sessions
        for session_dir in self.base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            session_file = session_dir / "session.json"
            data = self._read_json(session_file)
            if data:
                sessions.append(Session.from_dict(data))
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions[:limit]

    # ---- Message Append-Only Log ----

    def append_message(self, message: Message) -> None:
        """Append a message to the session JSONL log.

        Args:
            message: Message to append.
        """
        path = self._messages_file(message.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(message.to_dict(), ensure_ascii=False) + "\n"
        # Lock + fsync: prevents two writers from interleaving bytes inside
        # a single line and guarantees the line hits disk before the lock
        # releases so a reader that grabs the lock sees a complete entry.
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # Some filesystems (tmpfs, some WSL bind mounts) reject
                    # fsync — fall through; durability degrades to OS cache
                    # but ordering is still preserved by the lock.
                    pass

    def get_messages(self, session_id: str, limit: int = 100) -> List[Message]:
        """Read all messages for a session.

        Args:
            session_id: Session ID.
            limit: Maximum number of messages to return.

        Returns:
            List of Message objects in chronological order.
        """
        path = self._messages_file(session_id)
        if not path.exists():
            return []
        messages: List[Message] = []
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            if line.strip():
                messages.append(Message.from_dict(json.loads(line)))
        return messages[-limit:]

    # ---- Attempt CRUD ----

    def create_attempt(self, attempt: Attempt) -> Attempt:
        """Create an execution attempt.

        Args:
            attempt: Attempt to create.

        Returns:
            The persisted Attempt.
        """
        attempt_dir = self._attempt_dir(attempt.session_id, attempt.attempt_id)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(
            self._attempt_file(attempt.session_id, attempt.attempt_id),
            attempt.to_dict(),
        )
        return attempt

    def get_attempt(self, session_id: str, attempt_id: str) -> Optional[Attempt]:
        """Read an execution attempt.

        Args:
            session_id: Session ID.
            attempt_id: Attempt ID.

        Returns:
            The Attempt instance, or None when it does not exist.
        """
        path = self._attempt_file(session_id, attempt_id)
        data = self._read_json(path)
        if data is None:
            return None
        return Attempt.from_dict(data)

    def update_attempt(self, attempt: Attempt) -> None:
        """Update an execution attempt.

        Args:
            attempt: Modified Attempt.
        """
        self._write_json(
            self._attempt_file(attempt.session_id, attempt.attempt_id),
            attempt.to_dict(),
        )

    def list_attempts(self, session_id: str) -> List[Attempt]:
        """Return all attempts for a session in directory order.

        Args:
            session_id: Session ID.

        Returns:
            List of Attempt objects. Empty if the session has none.
        """
        attempts_root = self._session_dir(session_id) / "attempts"
        if not attempts_root.exists():
            return []
        out: List[Attempt] = []
        for child in attempts_root.iterdir():
            if not child.is_dir():
                continue
            data = self._read_json(child / "attempt.json")
            if data:
                try:
                    out.append(Attempt.from_dict(data))
                except (KeyError, TypeError, ValueError):
                    # Corrupt or schema-drifted attempt — skip rather than
                    # crash the whole listing. Logged by caller if needed.
                    continue
        return out

    def reconcile_stale_running(self, stale_threshold_seconds: int = 1800) -> int:
        """Mark RUNNING attempts that have been silent too long as FAILED.

        Architectural gap #6 (Codex audit): SessionService._run_attempt
        runs in an asyncio task; if the host process dies between
        mark_running() and mark_completed/mark_failed, the attempt stays
        RUNNING forever in the filesystem. SwarmStore solves the same
        class of bug via reconcile_run; this is the SessionStore equivalent.

        Called from SessionService.__init__ — one-shot on boot. Future
        improvement: heartbeat-based reaping like SwarmStore's reap_stale.

        Args:
            stale_threshold_seconds: How long a RUNNING attempt must be
                silent before being reaped. Default 30 minutes — anything
                shorter risks reaping legitimate long-running attempts.

        Returns:
            Number of attempts that were reaped.
        """
        from datetime import datetime as _dt

        if not self.base_dir.exists():
            return 0
        now = _dt.now()
        reaped = 0
        for session_dir in self.base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            attempts_root = session_dir / "attempts"
            if not attempts_root.exists():
                continue
            for attempt_dir in attempts_root.iterdir():
                if not attempt_dir.is_dir():
                    continue
                data = self._read_json(attempt_dir / "attempt.json")
                if not data or data.get("status") != AttemptStatus.RUNNING.value:
                    continue
                created_iso = data.get("created_at")
                if not created_iso:
                    continue
                try:
                    created = _dt.fromisoformat(created_iso)
                except ValueError:
                    continue
                age_s = (now - created).total_seconds()
                if age_s < stale_threshold_seconds:
                    continue
                try:
                    attempt = Attempt.from_dict(data)
                except (KeyError, TypeError, ValueError):
                    continue
                attempt.mark_failed(
                    error=(
                        f"reconciled on startup: attempt was RUNNING for "
                        f"{int(age_s)}s without progress (likely host crash)"
                    ),
                )
                self.update_attempt(attempt)
                reaped += 1
        return reaped

    # ---- IO Helpers ----

    @staticmethod
    def _write_json(path: Path, data: Dict[str, Any]) -> None:
        # Atomic: write to .tmp then rename under the module-level lock.
        # Readers either see the old file or the new file, never partial.
        # See module docstring for cross-process caveats.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        content = json.dumps(data, ensure_ascii=False, indent=2)
        with _WRITE_LOCK:
            tmp_path.write_text(content, encoding="utf-8")
            _replace_with_retry(tmp_path, path)

    @staticmethod
    def _read_json(path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
