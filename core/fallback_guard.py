"""Invalidate only pending extraction for body parts stopped by a later message."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Session:
    versions: dict[str, int] = field(default_factory=dict)
    pending: int = 0


@dataclass
class FallbackToken:
    key: tuple[str, str]
    session: _Session
    snapshot: dict[str, int]
    finished: bool = False


class FallbackStopGuard:
    def __init__(self, max_sessions: int = 128, max_pending: int = 128):
        self.max_sessions = max_sessions
        self.max_pending = max_pending
        self._sessions: dict[tuple[str, str], _Session] = {}

    def begin(self, character: str, session: str) -> FallbackToken | None:
        key = (str(character), str(session))
        group = self._sessions.get(key)
        if group is None:
            if len(self._sessions) >= self.max_sessions:
                return None
            group = self._sessions[key] = _Session()
        if group.pending >= self.max_pending:
            return None
        group.pending += 1
        return FallbackToken(key, group, dict(group.versions))

    def stop(self, character: str, session: str, parts) -> None:
        group = self._sessions.get((str(character), str(session)))
        if group is not None:
            for part in set(map(str, parts)):
                group.versions[part] = group.versions.get(part, 0) + 1

    def invalidated(self, token: FallbackToken, part: str) -> bool:
        return token.session.versions.get(str(part), 0) != token.snapshot.get(str(part), 0)

    def finish(self, token: FallbackToken) -> None:
        if token.finished:
            return
        token.finished = True
        token.session.pending -= 1
        if token.session.pending == 0 and self._sessions.get(token.key) is token.session:
            del self._sessions[token.key]
