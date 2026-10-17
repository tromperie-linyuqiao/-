"""A small, optional, in-memory contract for scoped environment observations.

Providers own their observations. This module does not import plugins, call a
model, persist history, or deliver messages. Reading a snapshot never renews
the age of its individual observations.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


API_VERSION = 1
STATUSES = frozenset({"fresh", "unknown", "stale", "unavailable", "disabled", "ambiguous", "conflict"})
_SCOPE_FIELDS = ("platform", "bot_id", "user_id", "character_id", "session_id")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,95}$")


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z") if ts is not None else None


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp needs a timezone")
            result = parsed.timestamp()
    else:
        raise ValueError("invalid timestamp")
    if not math.isfinite(result) or result < 0 or result > 253402300799:
        raise ValueError("timestamp out of range")
    return result


def _json_value(value: Any, *, max_depth: int = 12, max_bytes: int = 32768) -> Any:
    def visit(item: Any, depth: int) -> None:
        if depth > max_depth:
            raise ValueError("JSON value is too deep")
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non-finite JSON number")
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for child in item.values():
                visit(child, depth + 1)
            return
        raise ValueError("value must be plain JSON")

    visit(value, 0)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("JSON value is too large")
    return json.loads(encoded)


def normalize_scope(scope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(scope, dict):
        raise ValueError("scope must be a mapping")
    result: dict[str, Any] = {}
    for key in _SCOPE_FIELDS:
        item = scope.get(key)
        if item is not None:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                raise ValueError("scope identity must be a string or integer")
            item = str(item)
            if len(item) > 256 or any(ord(ch) < 32 for ch in item):
                raise ValueError("invalid scope identity")
            item = item or None
        result[key] = item
    private = scope.get("private", False)
    if not isinstance(private, bool):
        raise ValueError("private must be a boolean")
    result["private"] = private
    return result


def _event_call(event: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(event, name, default)
        return value() if callable(value) else value
    except Exception:
        return default


def scope_for_event(event: Any, character_id: str | None = None) -> dict[str, Any]:
    """Derive identity from AstrBot's public event API without importing it.

    Unknown values remain null. A group/session is never inferred to be a
    private conversation merely because an origin string is absent.
    """
    origin = _event_call(event, "unified_msg_origin")
    platform = _event_call(event, "get_platform_name") or _event_call(event, "get_platform_id")
    message = _event_call(event, "message_obj")
    bot_id = _event_call(event, "get_self_id") or getattr(message, "self_id", None)
    user_id = _event_call(event, "get_sender_id")
    session_id = origin or _event_call(event, "get_session_id")
    private = _event_call(event, "is_private_chat")
    if not isinstance(private, bool):
        group = _event_call(event, "get_group_id")
        message_type = _event_call(event, "get_message_type") or getattr(message, "type", None)
        kind = str(getattr(message_type, "value", message_type) or "").lower()
        private = not bool(group) and kind in {"friendmessage", "private", "private_message", "friend"}
        if not private and origin:
            private = ":friendmessage:" in str(origin).lower() or ":private_message:" in str(origin).lower()
    return normalize_scope({
        "platform": str(platform) if platform is not None else None,
        "bot_id": str(bot_id) if bot_id is not None else None,
        "user_id": str(user_id) if user_id is not None else None,
        "character_id": character_id,
        "session_id": str(session_id) if session_id is not None else None,
        "private": bool(private),
    })


@dataclass
class _Source:
    name: str
    owner: Any
    domains: frozenset[str]
    collector: Callable | None
    renderer: Callable | None
    enabled: Any
    priority: int
    generation: int
    last_enabled: bool | None = None


class PresenceLayer:
    """Versioned provider registry and bounded observation cache.

    ``update`` returns acceptance, while snapshot ``revision`` changes only
    when facts/availability change, not for a repeated transport timestamp.
    All returned values are detached JSON data.
    """

    api_version = API_VERSION

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 role_resolver: Callable | None = None, enabled: Any = None,
                 context_enabled: Any = None, context_renderer: Callable | None = None, max_sources: int = 32,
                 max_scopes: int = 256, max_records: int = 1024) -> None:
        self._clock = clock
        self.role_resolver = role_resolver
        self._enabled_callback = enabled
        self._context_enabled_callback = context_enabled
        self.context_renderer = context_renderer
        self._last_active: bool | None = None
        self.max_sources = min(max(1, int(max_sources)), 32)
        self.max_scopes = min(max(1, int(max_scopes)), 256)
        self.max_records = min(max(1, int(max_records)), 1024)
        self._sources: dict[str, _Source] = {}
        self._scopes: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._session_scopes: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._records: OrderedDict[tuple[str, str, str, str | None], dict[str, Any]] = OrderedDict()
        # Kept separately from displayed records: a complete provider snapshot
        # may hide an old device without allowing its late playback to revive.
        self._watermarks: OrderedDict[tuple[str, str, str, str | None], dict[str, Any]] = OrderedDict()
        self._revisions: dict[str, int] = {}
        self._view_fingerprints: dict[str, str] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._generation = 0
        self._epoch = 0
        self._closed = False
        self._metrics = {"accepted_updates": 0, "collector_failures": 0, "collector_timeouts": 0,
                         "shared_refreshes": 0}

    @staticmethod
    def _flag(callback: Any) -> bool:
        try:
            return True if callback is None else bool(callback() if callable(callback) else callback)
        except Exception:
            return False

    @property
    def active(self) -> bool:
        active = not self._closed and self._flag(self._enabled_callback)
        if not active and self._last_active is not False:
            self.clear()
        self._last_active = active
        return active

    @property
    def context_enabled(self) -> bool:
        return self.active and self._flag(self._context_enabled_callback)

    def scope_for_event(self, event: Any, character_id: str | None = None) -> dict[str, Any]:
        if character_id is None and callable(self.role_resolver):
            try:
                character_id = self.role_resolver(str(_event_call(event, "get_sender_id") or ""))
            except Exception:
                character_id = None
        return scope_for_event(event, character_id)

    def remember_scope(self, scope: dict[str, Any]) -> bool:
        """Lease a complete identity seen on a genuine private inbound event.

        The host must never call this with a fabricated wake-up event. Keeping
        only identities allows proactive calls to reuse the true bot/platform
        without storing message bodies or guessing account identifiers.
        """
        if not self.active:
            return False
        try:
            normalized = normalize_scope(scope)
            if not normalized["private"] or any(not normalized[field] for field in ("platform", "bot_id", "user_id", "session_id")):
                return False
            if callable(self.role_resolver):
                role = self.role_resolver(normalized["user_id"])
                if not isinstance(role, str) or not role:
                    return False
                normalized = normalize_scope({**normalized, "character_id": role})
            elif not normalized["character_id"]:
                return False
            session = normalized["session_id"]
            self._session_scopes[session] = normalized
            self._session_scopes.move_to_end(session)
            while len(self._session_scopes) > self.max_scopes:
                self._session_scopes.popitem(last=False)
            return True
        except Exception:
            return False

    def scope_for_session(self, umo: str) -> dict[str, Any] | None:
        """Resolve a known session with its current allowed role, fail closed."""
        if not self.active or not isinstance(umo, str):
            return None
        saved = self._session_scopes.get(umo)
        if saved is None:
            return None
        try:
            if callable(self.role_resolver):
                role = self.role_resolver(saved["user_id"])
                if not isinstance(role, str) or not role:
                    return None
            else:
                role = saved["character_id"]
                if not role:
                    return None
            current = normalize_scope({**saved, "character_id": role})
            self._session_scopes[umo] = current
            self._session_scopes.move_to_end(umo)
            return copy.deepcopy(current)
        except Exception:
            return None

    def register(self, source: str, *, owner: Any = None, domains: Any,
                 collector: Callable | None = None, renderer: Callable | None = None,
                 enabled: Any = None, priority: int = 0) -> bool:
        if self._closed or not isinstance(source, str) or not _IDENTIFIER.fullmatch(source):
            return False
        if isinstance(domains, str):
            return False
        try:
            domains = frozenset(domains)
            if not domains or len(domains) > 32 or any(not isinstance(d, str) or not _IDENTIFIER.fullmatch(d) for d in domains):
                return False
            if collector is not None and not callable(collector) or renderer is not None and not callable(renderer):
                return False
            if enabled is not None and not isinstance(enabled, bool) and not callable(enabled):
                return False
            if isinstance(priority, bool) or not isinstance(priority, int) or abs(priority) > 100000:
                return False
        except (ValueError, TypeError):
            return False
        existing = self._sources.get(source)
        if existing is None and len(self._sources) >= self.max_sources:
            return False
        self._purge_source(source)
        self._generation += 1
        self._sources[source] = _Source(source, owner, domains, collector, renderer, enabled, priority, self._generation)
        return True

    def unregister(self, source: str, owner: Any = None) -> bool:
        descriptor = self._sources.get(source)
        if descriptor is None or descriptor.owner is not owner:
            return False
        del self._sources[source]
        self._purge_source(source)
        self._generation += 1
        return True

    def _enabled(self, descriptor: _Source) -> bool:
        try:
            result = descriptor.enabled() if callable(descriptor.enabled) else descriptor.enabled
            result = True if descriptor.enabled is None else bool(result)
        except Exception:
            result = False
        if not result and descriptor.last_enabled is not False:
            self._generation += 1
            descriptor.generation = self._generation
            self._purge_source(descriptor.name)
        descriptor.last_enabled = result
        return result

    def _key(self, scope: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        normalized = normalize_scope(scope)
        return json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":")), normalized

    def _remember_scope(self, key: str, normalized: dict[str, Any]) -> None:
        if key in self._scopes:
            self._scopes.move_to_end(key)
            return
        while len(self._scopes) >= self.max_scopes:
            old_key, _ = self._scopes.popitem(last=False)
            self._purge_scope(old_key)
        self._scopes[key] = normalized
        self._revisions.setdefault(key, 0)

    def _changed(self, key: str) -> None:
        self._revisions[key] = self._revisions.get(key, 0) + 1
        self._view_fingerprints.pop(key, None)

    def _purge_scope(self, key: str) -> None:
        for record_key in list(self._records):
            if record_key[0] == key:
                del self._records[record_key]
        for record_key in list(self._watermarks):
            if record_key[0] == key:
                del self._watermarks[record_key]
        self._revisions.pop(key, None)
        self._view_fingerprints.pop(key, None)

    def _purge_source(self, source: str) -> None:
        changed = set()
        for key in list(self._records):
            if key[1] == source:
                changed.add(key[0])
                del self._records[key]
        for key in list(self._watermarks):
            if key[1] == source:
                del self._watermarks[key]
        for scope_key in changed:
            self._changed(scope_key)

    def update(self, source: str, scope: dict[str, Any], domain: str, value: Any, *,
               observed_at: Any = None, received_at: Any = None, ttl_seconds: float = 180,
               status: str = "fresh", evidence_kind: str = "observed", device_id: str | None = None,
               revision: Any = None, importance: str = "normal", change_key: Any = None) -> bool:
        if not isinstance(source, str) or not isinstance(domain, str):
            return False
        descriptor = self._sources.get(source)
        if not self.active or descriptor is None or domain not in descriptor.domains or not self._enabled(descriptor):
            return False
        try:
            key, normalized = self._key(scope)
            data = _json_value(value)
            now = _epoch(self._clock())
            received = _epoch(received_at) if received_at is not None else now
            # An unavailable sensor is a control signal, not a new observation
            # of its last song. Do not advance that song's source watermark.
            observed = _epoch(observed_at) if observed_at is not None else (received if status == "fresh" else None)
            ttl = float(ttl_seconds)
            if isinstance(ttl_seconds, bool) or not math.isfinite(ttl) or ttl < 0 or ttl > 604800 or status not in STATUSES:
                return False
            if received > now + 30 or (observed is not None and (observed > now + 30 or observed > received + 30)):
                return False
            if evidence_kind not in {"observed", "inferred", "simulated", "derived", "reported"} or importance not in {"normal", "important", "critical"}:
                return False
            if device_id is not None and (not isinstance(device_id, str) or not device_id or len(device_id) > 128):
                return False
            revision = _json_value(revision, max_bytes=1024)
            semantic_value = _json_value(change_key) if change_key is not None else data
            if isinstance(revision, (dict, list, bool)):
                return False
        except (ValueError, TypeError, OverflowError, RecursionError):
            return False
        record_key = (key, source, domain, device_id)
        old = self._records.get(record_key)
        fingerprint = self._fingerprint({"value": semantic_value, "status": status, "evidence_kind": evidence_kind,
                                         "device_id": device_id, "importance": importance})
        value_fingerprint = self._fingerprint(semantic_value)
        terminal = self._terminal_music(domain, data)
        watermark = self._watermarks.get(record_key) or (old.get("watermark") if old is not None else None)
        if watermark is not None:
            old_observed = watermark["observed"] if watermark is not None else None
            old_revision = watermark["source_revision"] if watermark is not None else None
            if observed is not None and old_observed is not None and observed < old_observed:
                return False
            if observed is not None and isinstance(revision, (int, float)) and isinstance(old_revision, (int, float)) and revision < old_revision:
                return False
            # Equal observations cannot resurrect music after an explicit pause,
            # or replace any other value with a late transport retry.
            higher_revision = isinstance(revision, (int, float)) and isinstance(old_revision, (int, float)) and revision > old_revision
            if observed is not None and observed == old_observed and not higher_revision:
                if watermark["terminal"] and self._playing_music(domain, data):
                    return False
                fact_changed = value_fingerprint != watermark["value_fingerprint"]
                availability_only = status != "fresh"
                if fact_changed and not terminal and not availability_only:
                    return False
        preserve_watermark = (watermark is not None and status != "fresh" and observed == watermark["observed"])
        if observed is not None and not preserve_watermark:
            watermark = {"observed": observed, "source_revision": revision, "terminal": terminal,
                         "value_fingerprint": value_fingerprint}
        self._remember_scope(key, normalized)
        if watermark is not None:
            self._watermarks[record_key] = watermark
            self._watermarks.move_to_end(record_key)
            while len(self._watermarks) > self.max_records:
                self._watermarks.popitem(last=False)
        if old is None:
            while len(self._records) >= self.max_records:
                evicted_key, _ = self._records.popitem(last=False)
                self._changed(evicted_key[0])
        self._records[record_key] = {"value": data, "source": source, "domain": domain, "status": status,
                                    "observed": observed, "received": received, "expires": observed + ttl if observed is not None else None,
                                    "evidence_kind": evidence_kind, "device_id": device_id,
                                    "source_revision": revision, "importance": importance,
                                    "fingerprint": fingerprint, "semantic_value": semantic_value, "watermark": watermark,
                                    "generation": descriptor.generation}
        self._records.move_to_end(record_key)
        if old is None or old["fingerprint"] != fingerprint:
            self._changed(key)
        self._metrics["accepted_updates"] += 1
        return True

    @staticmethod
    def _fingerprint(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _playing_music(domain: str, value: Any) -> bool:
        if domain != "music" or not isinstance(value, dict):
            return False
        state = value.get("user_state")
        return value.get("playing") is True or isinstance(state, dict) and state.get("playing") is True

    @staticmethod
    def _terminal_music(domain: str, value: Any) -> bool:
        if domain != "music" or not isinstance(value, dict):
            return False
        state = value.get("user_state")
        return (value.get("playing") is False or isinstance(state, dict) and state.get("playing") is False
                or value.get("event_type") in {"music_paused", "music_stopped"})

    def _public_record(self, record: dict[str, Any], now: float) -> dict[str, Any]:
        status = record["status"]
        if status == "fresh" and now >= record["expires"]:
            status = "stale"
        return {"value": copy.deepcopy(record["value"]), "source": record["source"], "status": status,
                "meta": {"observed_at": _iso(record["observed"]), "received_at": _iso(record["received"]),
                         "expires_at": _iso(record["expires"]), "source_revision": record["source_revision"],
                         "evidence_kind": record["evidence_kind"], "device_id": record["device_id"],
                         "importance": record["importance"], "change_key": copy.deepcopy(record["semantic_value"])}}

    def _domain_record(self, domain: str, now: float, available: dict[str, bool], records: list[dict[str, Any]]) -> dict[str, Any]:
        candidates = []
        for record in records:
            descriptor = self._sources.get(record["source"])
            if descriptor is not None and available.get(descriptor.name) and record["generation"] == descriptor.generation:
                public = self._public_record(record, now)
                candidates.append((public, descriptor.priority, record["observed"] or 0))
        if not candidates:
            providers = [d for d in self._sources.values() if domain in d.domains]
            disabled = bool(providers) and not any(available.get(d.name) for d in providers)
            return {"value": None, "source": None, "status": "disabled" if disabled else "unknown", "meta": {}}
        candidates.sort(key=lambda item: (item[0]["status"] == "fresh", item[1], item[2]), reverse=True)
        selected, priority, _ = candidates[0]
        fresh_peers = [item[0] for item in candidates if item[0]["status"] == "fresh" and item[1] == priority]
        if selected["status"] == "fresh" and any(peer["value"] != selected["value"] for peer in fresh_peers):
            selected["status"] = "conflict"
            selected["value"] = None
            selected["meta"]["conflicting_sources"] = sorted({peer["source"] for peer in fresh_peers})
        return selected

    def read(self, scope: dict[str, Any], domain: str | None = None, *, now: Any = None) -> dict[str, Any]:
        key, normalized = self._key(scope)
        ts = _epoch(now) if now is not None else _epoch(self._clock())
        active = self.active
        available = {name: self._enabled(descriptor) and active for name, descriptor in list(self._sources.items())}
        if domain is not None and (not isinstance(domain, str) or not _IDENTIFIER.fullmatch(domain)):
            raise ValueError("invalid domain")
        domains = {domain} if domain is not None else {d for descriptor in self._sources.values() for d in descriptor.domains}
        scoped_records: dict[str, list[dict[str, Any]]] = {}
        for record_key, current in self._records.items():
            if record_key[0] == key:
                scoped_records.setdefault(current["domain"], []).append(current)
        states = {name: self._domain_record(name, ts, available, scoped_records.get(name, [])) for name in sorted(domains)}
        fact_view = {name: {"value": record["meta"].get("change_key", record["value"]), "source": record["source"], "status": record["status"],
                            "evidence_kind": record["meta"].get("evidence_kind"),
                            "device_id": record["meta"].get("device_id")} for name, record in states.items()}
        # Only whole snapshots affect availability change detection. A filtered
        # read must not change a scope's revision back and forth.
        if domain is None and key in self._scopes:
            view = self._fingerprint(fact_view)
            previous = self._view_fingerprints.get(key)
            if previous is not None and previous != view:
                self._revisions[key] = self._revisions.get(key, 0) + 1
            self._view_fingerprints[key] = view
            self._scopes.move_to_end(key)
        return {"schema_version": API_VERSION, "generated_at": _iso(ts), "scope": normalized,
                "revision": self._revisions.get(key, 0), "states": states}

    def snapshot(self, scope: dict[str, Any], *, now: Any = None) -> dict[str, Any]:
        return self.read(scope, now=now)

    def source_for(self, scope: dict[str, Any], domain: str, *, now: Any = None) -> str | None:
        """Return the selected source name without exposing provider objects."""
        return self.read(scope, domain, now=now)["states"][domain]["source"]

    async def _collect_source(self, descriptor: _Source, scope: dict[str, Any], now: float, epoch: int, deadline: float,
                              collection: dict[str, bool]) -> None:
        generation = descriptor.generation
        try:
            if inspect.iscoroutinefunction(descriptor.collector):
                items = await descriptor.collector(copy.deepcopy(scope), now)
            else:
                items = await asyncio.to_thread(descriptor.collector, copy.deepcopy(scope), now)
                if inspect.isawaitable(items):
                    items = await items
            if not collection["valid"] or asyncio.get_running_loop().time() > deadline or not self.active or self._epoch != epoch or self._sources.get(descriptor.name) is not descriptor or not self._enabled(descriptor) or descriptor.generation != generation:
                return
            if not isinstance(items, (list, tuple)) or len(items) > 64:
                return
            accepted_devices: dict[str, set[str | None]] = {}
            for item in items:
                if not isinstance(item, dict) or "domain" not in item or "value" not in item:
                    continue
                options = {field: item[field] for field in ("observed_at", "received_at", "ttl_seconds", "status", "evidence_kind", "device_id", "revision", "importance", "change_key") if field in item}
                if self.update(descriptor.name, scope, item["domain"], item["value"], **options):
                    accepted_devices.setdefault(item["domain"], set()).add(item.get("device_id"))
            # Collectors return the source's selected snapshot for each domain.
            # Prune only after accepted observations, so a rejected old frame
            # cannot erase the newer device; a multi-device batch stays intact.
            scope_key, _ = self._key(scope)
            changed = False
            for key in list(self._records):
                if (key[0] == scope_key and key[1] == descriptor.name and key[2] in accepted_devices
                        and key[3] not in accepted_devices[key[2]]):
                    del self._records[key]
                    changed = True
            if changed:
                self._changed(scope_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A sensor failure cannot break a normal conversation or erase
            # valid observations from unrelated providers.
            self._metrics["collector_failures"] += 1
            return

    @staticmethod
    def _consume_task(task: asyncio.Task) -> None:
        if not task.cancelled():
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass

    async def _refresh(self, scope: dict[str, Any], now: float, timeout: float) -> None:
        epoch = self._epoch
        deadline = asyncio.get_running_loop().time() + timeout
        collection = {"valid": True}
        tasks = [asyncio.create_task(self._collect_source(d, scope, now, epoch, deadline, collection))
                 for d in list(self._sources.values()) if self._enabled(d) and d.collector is not None]
        if not tasks:
            return
        try:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            self._metrics["collector_timeouts"] += len(pending)
        finally:
            collection["valid"] = False
            for task in tasks:
                if not task.done():
                    task.cancel()
                task.add_done_callback(self._consume_task)

    async def refresh(self, scope: dict[str, Any], *, now: Any = None, timeout_seconds: float = 2) -> dict[str, Any]:
        key, normalized = self._key(scope)
        ts = _epoch(now) if now is not None else _epoch(self._clock())
        timeout = min(max(float(timeout_seconds), 0.01), 5.0)
        if not math.isfinite(timeout):
            timeout = 2.0
        if self.active:
            task = self._inflight.get(key)
            if task is None or task.done():
                if len(self._inflight) >= self.max_scopes:
                    return self.snapshot(normalized, now=ts)
                task = asyncio.create_task(self._refresh(normalized, ts, timeout))
                self._inflight[key] = task
                def finished(done: asyncio.Task) -> None:
                    if self._inflight.get(key) is done:
                        self._inflight.pop(key, None)
                    self._consume_task(done)
                task.add_done_callback(finished)
            else:
                self._metrics["shared_refreshes"] += 1
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout + 0.1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Caller cancellation still propagates; a sensor timeout does
                # not cancel another caller's shared collection operation.
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    raise
        return self.snapshot(normalized, now=ts)

    def render(self, scope: dict[str, Any], domain: str, *, query: str = "", max_chars: int = 360, now: Any = None) -> str:
        snapshot = self.read(scope, domain, now=now)
        record = snapshot["states"][domain]
        descriptor = self._sources.get(record["source"])
        if descriptor is None or descriptor.renderer is None or record["status"] in {"disabled", "unknown", "unavailable", "ambiguous", "conflict"}:
            return ""
        maximum = min(max(int(max_chars), 0), 4096)
        try:
            rendered = descriptor.renderer(domain, copy.deepcopy(record), snapshot["scope"], str(query or "")[:2048], maximum,
                                           _epoch(now) if now is not None else _epoch(self._clock()))
            return rendered if isinstance(rendered, str) and len(rendered) <= maximum else ""
        except Exception:
            return ""

    def render_context(self, scope: dict[str, Any], *, query: str = "", max_chars: int = 420,
                       now: Any = None, channel: str = "ordinary") -> str:
        """Project context through the host's policy, without collecting again.

        Callers can await ``refresh`` first. The host owns role permissions and
        per-channel deduplication; the core supplies only the stable contract.
        """
        if not self.context_enabled or not callable(self.context_renderer):
            return ""
        try:
            normalized = normalize_scope(scope)
            maximum = min(max(int(max_chars), 0), 1200)
            ts = _epoch(now) if now is not None else _epoch(self._clock())
            if not isinstance(channel, str) or not _IDENTIFIER.fullmatch(channel):
                return ""
            rendered = self.context_renderer(normalized, query=str(query or "")[:2048],
                                             max_chars=maximum, now=ts, channel=channel)
            return rendered if isinstance(rendered, str) and len(rendered) <= maximum else ""
        except Exception:
            return ""

    def clear(self, scope: dict[str, Any] | None = None, *, source: str | None = None) -> None:
        """Invalidate cached data and any collection that began before clear."""
        self._epoch += 1
        self._session_scopes.clear()
        if source is not None:
            self._purge_source(source)
        elif scope is not None:
            key, _ = self._key(scope)
            self._purge_scope(key)
            self._scopes.pop(key, None)
        else:
            self._records.clear()
            self._watermarks.clear()
            self._scopes.clear()
            self._revisions.clear()
            self._view_fingerprints.clear()

    def diagnostics(self) -> dict[str, Any]:
        active = self.active
        return {"api_version": self.api_version, "active": active, "context_enabled": self.context_enabled,
                "registered_sources": [{"source": name, "domains": sorted(d.domains),
                                        "enabled": self._enabled(d) and active}
                                       for name, d in sorted(self._sources.items())],
                "cached_scopes": len(self._scopes), "cached_records": len(self._records),
                "cached_watermarks": len(self._watermarks),
                "known_private_sessions": len(self._session_scopes),
                "revision": sum(self._revisions.values()), "metrics": dict(self._metrics)}

    def close(self) -> None:
        self._closed = True
        self.clear()
        self._sources.clear()
        for task in list(self._inflight.values()):
            task.cancel()
        self._inflight.clear()
