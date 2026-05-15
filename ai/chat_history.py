"""
Persistent chat session storage for the AI assistant.

Sessions are stored as JSON files under::

    ~/.netscope/chats/<session_id>.json

An optional sync directory (set via Settings → AI → "Chat sync folder")
lets users point to a cloud-synced folder (Dropbox, OneDrive, etc.) so
history is accessible across machines without any account or API key.
When set, the sync folder is used exclusively; otherwise the local
default is used.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils import settings as _settings

_LOCAL_CHATS_DIR = Path(_settings.settings_dir_path()) / "chats"


# ── Data model ─────────────────────────────────────────────────────────────


@dataclass
class ChatMessage:
    role: str          # "user" | "assistant"
    content: str
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content, "timestamp": self.timestamp}

    @classmethod
    def from_dict(cls, d: dict) -> "ChatMessage":
        return cls(
            role=d.get("role", "user"),
            content=d.get("content", ""),
            timestamp=float(d.get("timestamp", 0.0)),
        )


@dataclass
class ChatSession:
    id: str
    title: str
    created_at: float
    updated_at: float
    messages: list[ChatMessage] = field(default_factory=list)

    @classmethod
    def new(cls) -> "ChatSession":
        now = time.time()
        return cls(id=str(uuid.uuid4()), title="New chat",
                   created_at=now, updated_at=now)

    @property
    def is_empty(self) -> bool:
        return not self.messages

    @property
    def preview(self) -> str:
        """First ~50 chars of the first user message, for list display."""
        for msg in self.messages:
            if msg.role == "user":
                t = msg.content.strip()
                return (t[:50] + "…") if len(t) > 50 else t
        return "Empty chat"

    @property
    def message_count(self) -> int:
        return len([m for m in self.messages if m.role == "user"])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChatSession":
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            title=d.get("title", "Chat"),
            created_at=float(d.get("created_at", time.time())),
            updated_at=float(d.get("updated_at", time.time())),
            messages=[ChatMessage.from_dict(m) for m in d.get("messages", [])],
        )


def auto_title(user_msg: str) -> str:
    """Generate a short session title from the first user message."""
    words = user_msg.strip().split()
    title = " ".join(words[:7])
    if len(words) > 7:
        title += "…"
    return title or "Chat"


def relative_time(ts: float) -> str:
    """Human-readable relative time, e.g. '3m ago', 'Yesterday', 'Jan 12'."""
    delta = time.time() - ts
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    days = int(delta / 86400)
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    return datetime.fromtimestamp(ts).strftime("%b %d")


# ── Manager ────────────────────────────────────────────────────────────────


class ChatHistoryManager:
    """Reads and writes chat sessions to a local (or cloud-sync) folder.

    The active directory is resolved on each call so that changing
    the sync folder in Settings takes effect immediately without
    restarting the app.
    """

    def _dir(self) -> Path:
        """Return the active chats directory, creating it if needed."""
        sync = (_settings.get("chat_sync_dir") or "").strip()
        if sync:
            p = Path(sync)
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except Exception:
                pass
        _LOCAL_CHATS_DIR.mkdir(parents=True, exist_ok=True)
        return _LOCAL_CHATS_DIR

    def _path(self, session_id: str) -> Path:
        return self._dir() / f"{session_id}.json"

    # ── Read ──────────────────────────────────────────────────────

    def list_sessions(self) -> list[ChatSession]:
        """Return all sessions, newest (by updated_at) first."""
        sessions: list[ChatSession] = []
        try:
            for f in self._dir().glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    sessions.append(ChatSession.from_dict(data))
                except Exception:
                    pass
        except Exception:
            pass
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def load_session(self, session_id: str) -> Optional[ChatSession]:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            return ChatSession.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception:
            return None

    # ── Write ─────────────────────────────────────────────────────

    def save_session(self, session: ChatSession) -> None:
        if session.is_empty:
            return
        try:
            self._path(session.id).write_text(
                json.dumps(session.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def delete_session(self, session_id: str) -> None:
        try:
            p = self._path(session_id)
            if p.exists():
                p.unlink()
        except Exception:
            pass

    # ── Export / import ───────────────────────────────────────────

    def export_to_file(self, session_id: str, dest: str) -> bool:
        session = self.load_session(session_id)
        if not session:
            return False
        try:
            Path(dest).write_text(
                json.dumps(session.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except Exception:
            return False

    def import_from_file(self, src: str) -> Optional[ChatSession]:
        try:
            data = json.loads(Path(src).read_text(encoding="utf-8"))
            session = ChatSession.from_dict(data)
            session.id = str(uuid.uuid4())  # fresh ID to avoid collisions
            self.save_session(session)
            return session
        except Exception:
            return None
