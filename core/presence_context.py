"""Request-local, bounded context selection over the shared Presence API.

No persona replacement, history writes, model calls or delivery. Provider
renderers remain the owners of domain wording and metadata sanitization.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import re
from typing import Any

CONTEXT_START = '<!-- virtual_presence_context_v1 -->'
CONTEXT_END = '<!-- /virtual_presence_context_v1 -->'
_GUARD = '仅作当前背景；相关时自然回应，不播报内部状态，不补造未知信息。'
_TOPICS = {
    'music': ('音乐', '歌曲', '听歌', '这首', '曲目', '歌手', '网易云', '在听', '放歌'),
    'weather': ('天气', '下雨', '雨天', '气温', '温度', '冷不冷', '热不热', '晴天', '风大'),
    'time': ('几点', '时间', '现在多晚', '星期', '几号', '今天日期'),
    'device.battery': ('手机', '设备', '电量', '充电', '电池'),
    'device.screen': ('手机', '设备', '屏幕', '亮屏', '灭屏'),
    'device.network': ('手机', '设备', '网络', '联网', '断网'),
    'device.app': ('手机', '设备', '应用', '在玩', '在用', '忙'),
    'device.audio': ('手机', '设备', '耳机', '音量'),
    'device.device': ('手机', '设备', '状态'),
    'device.status': ('手机', '设备', '在线', '离线', '状态'),
    'user_activity': ('状态', '在做', '忙', '闲置', '步数', '走了', '活动'),
    'reading': ('书', '阅读', '读到', '小说'),
    'virtual.body': ('身体', '体感', '哪里', '状态'),
    'virtual.space': ('在哪', '哪里', '房间', '位置', '庄园'),
}
_UNCERTAIN = frozenset({'unknown', 'stale', 'unavailable', 'disabled', 'ambiguous', 'conflict'})


def strip_context(text: str) -> str:
    return re.sub(re.escape(CONTEXT_START) + r'.*?' + re.escape(CONTEXT_END), '', str(text or ''), flags=re.S).strip()


def drop_request_context(req: Any) -> list[str]:
    """Remove owned fragments and return them as retry projection identities."""
    parts = getattr(req, 'extra_user_content_parts', None)
    if isinstance(parts, list):
        previous = [str(getattr(part, 'text', '')) for part in parts
                    if CONTEXT_START in str(getattr(part, 'text', ''))]
        req.extra_user_content_parts = [part for part in parts if CONTEXT_START not in str(getattr(part, 'text', ''))]
        return previous
    return []


def _key(scope: dict) -> str:
    return hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _signature(domain: str, record: dict) -> str:
    meta = record.get('meta') or {}
    value = meta.get('change_key', record.get('value'))
    # The public value can keep current playback progress. Its transport age
    # and ticking progress must not consume another background injection.
    if domain == 'music' and isinstance(value, dict) and 'user_state' in value:
        user = value.get('user_state') or {}
        song = value.get('song_info') or {}
        value = {'event_type': value.get('event_type'), 'song_id': value.get('song_id'),
                 'title': song.get('title'), 'artist': song.get('artist'),
                 'playing': user.get('playing'), 'reliable': user.get('reliable'),
                 'device_id': user.get('device_id'), 'source': user.get('source'),
                 'package': user.get('package'), 'track_id': user.get('track_id')}
    return _key({'domain': domain, 'source': record.get('source'), 'status': record.get('status'), 'value': value})


class PresenceContextPolicy:
    def __init__(self, max_scopes: int = 256):
        self._sent: OrderedDict[str, dict[str, tuple[str, float]]] = OrderedDict()
        # A request retry must refresh its existing temporary facts even when
        # ordinary cross-request dedupe suppresses them. Keep only digests and
        # domain names; never retain requests, history or rendered observations.
        self._projections: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self.max_scopes = max(1, min(int(max_scopes), 256))
        self.metrics = {'injections': 0, 'suppressed_repeats': 0, 'last_chars': 0,
                        'total_chars': 0, 'last_domains': [], 'queries': 0}

    def clear(self) -> None:
        self._sent.clear()
        self._projections.clear()

    def retry_domains(self, scope: dict, fragments: list[str]) -> frozenset[str]:
        domains: set[str] = set()
        for fragment in fragments:
            if isinstance(fragment, str) and len(fragment) <= 1200:
                domains.update(self._projections.get(_key({'scope': scope, 'projection': fragment}), ()))
        return frozenset(domains)

    @staticmethod
    def related(domain: str, text: str) -> bool:
        return any(word in str(text or '').lower() for word in _TOPICS.get(domain, ()))

    def build(self, api: Any, scope: dict, snapshot: dict, *, query: str = '',
              max_chars: int = 420, repeat_seconds: float = 300, now: float,
              force: bool = False, aspect: str = '',
              retry_domains: frozenset[str] = frozenset()) -> tuple[str, list[tuple[str, str]]]:
        maximum = max(0, min(int(max_chars), 1200))
        refresh = max(30, min(float(repeat_seconds), 3600))
        key = _key(scope)
        previous = self._sent.get(key, {})
        states = snapshot.get('states') if isinstance(snapshot, dict) else {}
        if not isinstance(states, dict):
            return '', []
        candidates = []
        for domain, record in states.items():
            if not isinstance(record, dict):
                continue
            # Existing body/space blocks keep their original gates and budgets.
            # The unified view exposes them but never duplicates them here.
            if domain.startswith('virtual.') and not force:
                continue
            status = record.get('status', 'unknown')
            signature = _signature(domain, record)
            prior = previous.get(domain)
            changed = prior is None or prior[0] != signature
            related = self.related(domain, query)
            targeted = bool(aspect and (domain == aspect or domain.startswith(aspect + '.') or self.related(domain, aspect)))
            if force and aspect and not targeted:
                continue
            importance = (record.get('meta') or {}).get('importance', 'normal')
            important = importance in {'important', 'critical'}
            if status in _UNCERTAIN:
                # An unavailable transition only needs to invalidate previous
                # music knowledge. Do not narrate absent sensors every turn.
                invalidates = domain == 'music' and prior is not None and changed
                eligible = related or force or invalidates or domain in retry_domains
            else:
                eligible = (force or related or domain in retry_domains
                            or (important and (changed or prior is None or now - prior[1] >= refresh)))
            if not eligible:
                if important and prior and not changed:
                    self.metrics['suppressed_repeats'] += 1
                continue
            rank = (0 if domain == 'music' else 1 if importance == 'critical' else 2 if related or force else 3, domain)
            candidates.append((rank, domain, record, signature))
        candidates.sort()
        overhead = len(CONTEXT_START) + len(CONTEXT_END) + len(_GUARD) + 4
        available = maximum - overhead
        chunks, selected = [], []
        for _, domain, record, signature in candidates:
            remaining = available - sum(len(c) for c in chunks) - len(chunks)
            if remaining < 20:
                break
            if record.get('status') in _UNCERTAIN:
                if domain == 'music':
                    text = '当前音乐无法确认；不要把此前歌曲当作正在播放，也不要据歌曲推断用户情绪。'
                elif domain == 'weather':
                    text = '当前天气没有有效观测，不能把未知或过期信息当作现在的天气。'
                elif domain.startswith('device.') or domain == 'user_activity':
                    text = '当前设备或活动信息无法确认。'
                else:
                    text = ''
            else:
                text = api.render(scope, domain, query=query, max_chars=remaining, now=now)
            if not text or len(text) > remaining:
                continue
            # Prevent duplicate generic unavailable device statements and
            # overlapping provider projections within the same request.
            if text in chunks:
                selected.append((domain, signature))
                continue
            chunks.append(text)
            selected.append((domain, signature))
        if not chunks:
            return '', []
        result = '\n'.join([CONTEXT_START, *chunks, _GUARD, CONTEXT_END])
        return (result, selected) if len(result) <= maximum else ('', [])

    def committed(self, scope: dict, selected: list[tuple[str, str]], *, now: float, chars: int,
                  projection: str = '') -> None:
        key = _key(scope)
        previous = self._sent.setdefault(key, {})
        self._sent.move_to_end(key)
        for domain, signature in selected:
            previous[domain] = (signature, now)
        while len(self._sent) > self.max_scopes:
            self._sent.popitem(last=False)
        if projection:
            projection_key = _key({'scope': scope, 'projection': projection})
            self._projections[projection_key] = tuple(domain for domain, _ in selected)
            self._projections.move_to_end(projection_key)
            while len(self._projections) > self.max_scopes:
                self._projections.popitem(last=False)
        self.metrics['injections'] += 1
        self.metrics['last_chars'] = chars
        self.metrics['total_chars'] += chars
        self.metrics['last_domains'] = [domain for domain, _ in selected]
