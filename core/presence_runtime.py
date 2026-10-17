"""Thin AstrBot host adapter for the optional shared Presence contract."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import re
import time
from typing import Any

from .presence import PresenceLayer
from .presence_context import PresenceContextPolicy, drop_request_context
from . import mood_signals, decay


def _text(value: Any, maximum: int = 100) -> str:
    return ' '.join(re.sub(r'[\x00-\x1f\x7f<>]', ' ', str(value or '')).split())[:maximum]


class PresenceIntegration:
    def __init__(self, plugin: Any, context: Any):
        self.plugin, self.context = plugin, context
        self.policy = PresenceContextPolicy()
        self.api = None
        self.start()

    def _enabled(self) -> bool:
        enabled = not getattr(self.plugin, '_stopping', False) and self.plugin._cfg_bool('presence_layer_enabled', True)
        if not enabled:
            self.policy.clear()
        return enabled

    def _context_enabled(self) -> bool:
        enabled = self._enabled() and self.plugin._cfg_bool('presence_context_enabled', True)
        if not enabled:
            self.policy.clear()
        return enabled

    def _role(self, user: str) -> str | None:
        resolved = self.plugin._resolve_session(user)
        return resolved[0] if resolved is not None else None

    def start(self) -> None:
        if self.api is not None:
            return
        self.api = PresenceLayer(role_resolver=self._role, enabled=self._enabled,
                                 context_enabled=self._context_enabled, context_renderer=self._project_context)
        self.api.register('presence.environment', owner=self, domains=('time', 'weather'),
                          collector=self._collect_environment, renderer=self._render_environment, enabled=self._enabled)
        self.api.register('presence.reading',owner=self,domains=('reading',),collector=self._collect_reading,
                          renderer=self._render_environment,
                          enabled=lambda:self._enabled() and self.plugin._cfg_bool('mo_enabled',True))
        self.api.register('presence.body',owner=self,domains=('virtual.body',),renderer=self._render_virtual,
                          enabled=lambda:self._enabled() and self.plugin._cfg_bool('vb_enabled',True))
        self.api.register('presence.space',owner=self,domains=('virtual.space',),renderer=self._render_virtual,
                          enabled=lambda:self._enabled() and self.plugin._space_enabled())
        setattr(self.context, 'presence_layer', self.api)

    def close(self) -> None:
        if self.api is not None:
            self.api.close()
            if getattr(self.context, 'presence_layer', None) is self.api:
                delattr(self.context, 'presence_layer')
        self.api = None
        self.policy.clear()

    def scope(self, event: Any) -> dict | None:
        if self.api is None or not self.api.active:
            return None
        if getattr(event, 'presence_proactive_framework', False):
            scope = self.api.scope_for_session(str(getattr(event, 'unified_msg_origin', '') or ''))
            if scope is None:
                return None
        else:
            scope = self.api.scope_for_event(event)
        if not scope.get('private') or not scope.get('character_id') or not scope.get('user_id'):
            return None
        if not getattr(event, 'presence_proactive_framework', False):
            self.api.remember_scope(scope)
        return scope

    def remember_event(self, event: Any) -> None:
        try:
            if not getattr(event, 'presence_proactive_framework', False):
                self.scope(event)
        except Exception:
            return

    def _context_max_chars(self) -> int:
        value = self.plugin._cfg_int('presence_context_max_chars', 420)
        return value if 160 <= value <= 1200 else 420

    def _repeat_seconds(self) -> int:
        value = self.plugin._cfg_int('presence_repeat_seconds', 300)
        return value if 30 <= value <= 3600 else 300

    def _prepare_context(self, scope: dict, *, query: str = '', max_chars: int = 420,
                         now: float, channel: str = 'ordinary',
                         previous: list[str] | None = None) -> tuple[str, list, dict]:
        memory_scope = dict(scope, _presence_channel=str(channel)[:64])
        if (self.api is None or not self.api.context_enabled or not scope.get('private')
                or self._role(str(scope.get('user_id') or '')) != scope.get('character_id')):
            return '', [], memory_scope
        maximum = min(max_chars, self._context_max_chars())
        if maximum < 160:
            return '', [], memory_scope
        # Dedupe state is specific to a generation outlet, while the facts and
        # identity remain a single shared snapshot. The API ignores this
        # private policy key when reading provider records.
        block, selected = self.policy.build(self.api,memory_scope,self.api.snapshot(scope,now=now),
            query=query, max_chars=maximum, repeat_seconds=self._repeat_seconds(), now=now,
            retry_domains=self.policy.retry_domains(memory_scope, previous or []))
        return block, selected, memory_scope

    def _project_context(self, scope: dict, *, query: str = '', max_chars: int = 420,
                         now: float, channel: str = 'ordinary') -> str:
        block, selected, memory_scope = self._prepare_context(
            scope, query=query, max_chars=max_chars, now=now, channel=channel)
        if block:
            self.policy.committed(memory_scope, selected, now=now, chars=len(block), projection=block)
        return block

    async def _collect_environment(self, scope: dict, now: float) -> list[dict]:
        if not scope.get('private') or self._role(str(scope.get('user_id') or '')) != scope.get('character_id'):
            return []
        offset = self.plugin._tz()
        tz = timezone(timedelta(hours=offset)) if offset is not None else datetime.now().astimezone().tzinfo
        local = datetime.fromtimestamp(now, tz)
        clock = local.strftime('%Y-%m-%d %H:%M') + ' 星期' + '一二三四五六日'[local.weekday()]
        rows = [{'domain':'time', 'value':{'local':clock, 'utc_offset_hours':offset},
                 'observed_at':now, 'ttl_seconds':90, 'evidence_kind':'observed'}]
        data_dir = str(getattr(self.plugin, '_space_data_dir', '') or '')
        weather = await asyncio.to_thread(mood_signals.read_weather, data_dir, now=now) if data_dir else None
        weather = weather if isinstance(weather, dict) else {}
        fresh = weather.get('available') is True
        observed = weather.get('observed_at')
        if observed is None:
            observed = mood_signals._weather_observed_at(weather.get('updated_at'))
        rows.append({'domain':'weather', 'value':{'city':_text(weather.get('city'),60),
                                                'condition':_text(weather.get('condition'),120) if fresh else None,
                                                'is_rain':weather.get('is_rain') if fresh else None,
                                                'location_scope':'last_weather_query'},
                     'status':'fresh' if fresh else 'stale' if weather.get('stale') else 'unknown',
                     'observed_at':observed, 'ttl_seconds':21600, 'evidence_kind':'observed'})
        return rows

    async def _collect_reading(self, scope: dict, now: float) -> list[dict]:
        if not scope.get('private') or self._role(str(scope.get('user_id') or '')) != scope.get('character_id'):
            return []
        rows=[]
        # Reuse the existing tick sample instead of re-scanning all book
        # chapter files on each ordinary chat request.
        reading = (getattr(self.plugin, '_last_signals', {}) or {}).get('reading')
        reading_at = decay.parse_ts((getattr(self.plugin, '_tick_health', {}) or {}).get('last_at'))
        if isinstance(reading, dict) and reading.get('available') is True:
            rows.append({'domain':'reading', 'value':reading, 'observed_at':reading_at,
                         'ttl_seconds':90, 'evidence_kind':'inferred'})
        else:
            rows.append({'domain':'reading', 'value':None, 'observed_at':None,
                         'status':'unknown', 'ttl_seconds':90, 'evidence_kind':'inferred'})
        return rows

    @staticmethod
    def _render_environment(domain: str, record: dict, scope: dict, query: str, maximum: int, now: float) -> str:
        value = record.get('value') or {}
        if record.get('status') != 'fresh' or not isinstance(value, dict):
            return ''
        if domain == 'time':
            text = '当前服务器时间：' + _text(value.get('local'),50) + '；不据时间猜测用户活动。'
        elif domain == 'weather':
            city, condition = _text(value.get('city'),40), _text(value.get('condition'),100)
            if not condition:
                return ''
            text = ('最近有效天气查询' + ('（'+city+'）' if city else '') + '：' + condition +
                    '；查询地点不自动代表用户当前位置。')
        elif domain == 'reading':
            name = _text(value.get('book_title'),60)
            text = ('书柜资料消化进度：'+name if name else '有可用的书柜资料消化进度') + '；不猜测用户正在阅读。'
        else:
            return ''
        return text if len(text) <= maximum else ''

    @staticmethod
    def _render_virtual(domain: str, record: dict, scope: dict, query: str, maximum: int, now: float) -> str:
        value = record.get('value') or {}
        text = value.get('summary') if isinstance(value, dict) and record.get('status') == 'fresh' else ''
        return text if isinstance(text,str) and len(text) <= maximum else ''

    def publish_virtual(self, event: Any, domain: str, value: dict, *, now: float) -> None:
        try:
            scope = self.scope(event)
            if scope is not None:
                self.api.update('presence.body' if domain=='virtual.body' else 'presence.space', scope, domain, value, observed_at=now,
                                ttl_seconds=180, evidence_kind='simulated')
        except Exception:
            # A failed optional projection must not suppress the existing
            # body/space injection or change its persistence path.
            return

    async def inject(self, event: Any, req: Any, text_part: Any) -> None:
        previous = drop_request_context(req)
        scope = self.scope(event)
        if scope is None or self.api is None or not self.api.context_enabled or text_part is None:
            return
        if not isinstance(getattr(req, 'extra_user_content_parts', None), list):
            return
        started = self.api
        await started.refresh(scope, timeout_seconds=2)
        now = time.time()
        # Authorization/binding can change while a device request is pending.
        if self.api is not started or not started.context_enabled or self.scope(event) != scope:
            return
        # Another invocation can share the collection while using this same
        # request. Re-read its current list after the await, so only one owned
        # fragment survives and we never append to a detached, obsolete list.
        previous.extend(drop_request_context(req))
        parts = getattr(req, 'extra_user_content_parts', None)
        if not isinstance(parts, list):
            return
        synthetic = getattr(event,'presence_proactive_framework',False)
        block, selected, memory_scope = self._prepare_context(scope,
            query='' if synthetic else str(getattr(event,'message_str','') or ''),
            max_chars=self._context_max_chars(), now=now,
            channel='pc_framework' if synthetic else 'ordinary', previous=previous)
        if block:
            parts.append(text_part(text=block).mark_as_temp())
            self.policy.committed(memory_scope, selected, now=now, chars=len(block), projection=block)

    async def query(self, event: Any, aspect: str = '') -> str:
        scope = self.scope(event)
        if scope is None or self.api is None:
            return '当前会话没有可用的环境感知权限。'
        started = self.api
        await started.refresh(scope, timeout_seconds=2)
        now = time.time()
        if self.api is not started or not started.active or self.scope(event) != scope:
            return '当前环境信息暂不可用。'
        block, _ = self.policy.build(started,scope,started.snapshot(scope,now=now),
                                     query=str(aspect or ''), aspect=str(aspect or '')[:128],
                                     max_chars=800,now=now,force=True)
        self.policy.metrics['queries'] += 1
        if not block:
            return '没有这个方面的有效环境观测；未知不代表未播放、离线或晴天。'
        # Tool results are evidence, not an extra persona prompt.
        from .presence_context import CONTEXT_START, CONTEXT_END
        return block.replace(CONTEXT_START,'').replace(CONTEXT_END,'').strip()

    def diagnostics(self) -> dict:
        result = self.api.diagnostics() if self.api is not None else {'active':False,'context_enabled':False,'registered_sources':[]}
        result['context'] = dict(self.policy.metrics)
        result['context']['max_chars'] = self._context_max_chars()
        result['context']['repeat_seconds'] = self._repeat_seconds()
        return result
