"""Meaningful selection and host contracts; synthetic data, no live SDK."""
import asyncio
from types import SimpleNamespace

from astrbot_plugin_virtual_body.core.presence import PresenceLayer
from astrbot_plugin_virtual_body.core.presence_context import (
    CONTEXT_START, CONTEXT_END, PresenceContextPolicy, drop_request_context, strip_context,
)
from astrbot_plugin_virtual_body.core.presence_runtime import PresenceIntegration


SCOPE = {'platform':'test','bot_id':'bot','user_id':'peer','character_id':'role_a',
         'session_id':'test:FriendMessage:peer','private':True}


def registry():
    layer = PresenceLayer(clock=lambda:1000)
    def render(domain, record, scope, query, max_chars, now):
        value = record['value']
        if domain == 'music':
            text = ('音乐已暂停；不沿用旧曲。' if value.get('event_type')=='music_paused'
                    else '当前音乐：'+str(value.get('title',''))+'；音乐仅作背景，不推断用户情绪。')
        else:
            text = domain+' '+str(value)
        return text if len(text)<=max_chars else ''
    layer.register('test.sensor',domains=('music','weather','time','device.battery','virtual.body'),renderer=render)
    return layer


def update(layer, domain, value, *, importance='normal', change_key=None):
    assert layer.update('test.sensor',SCOPE,domain,value,observed_at=1000,ttl_seconds=180,
                        importance=importance,change_key=change_key)


def test_ordinary_time_and_weather_add_zero_context():
    layer=registry(); policy=PresenceContextPolicy()
    update(layer,'time',{'local':'08:00'}); update(layer,'weather',{'rain':True})
    block,_=policy.build(layer,SCOPE,layer.snapshot(SCOPE),query='早上好',now=1000)
    assert block==''


def test_important_music_changes_not_ticking_progress():
    layer=registry(); policy=PresenceContextPolicy()
    update(layer,'music',{'title':'A','progress':1},importance='important',change_key={'title':'A'})
    block,selected=policy.build(layer,SCOPE,layer.snapshot(SCOPE),query='你好',now=1000)
    assert '当前音乐：A' in block
    policy.committed(SCOPE,selected,now=1000,chars=len(block))
    assert layer.update('test.sensor',SCOPE,'music',{'title':'A','progress':20},observed_at=1001,
                        received_at=1001,importance='important',change_key={'title':'A'})
    block,_=policy.build(layer,SCOPE,layer.snapshot(SCOPE,now=1001),query='再说一句',now=1001)
    assert block==''
    assert layer.update('test.sensor',SCOPE,'music',{'title':'B'},observed_at=1002,received_at=1002,
                        importance='important',change_key={'title':'B'})
    block,_=policy.build(layer,SCOPE,layer.snapshot(SCOPE,now=1002),query='继续',now=1002)
    assert '当前音乐：B' in block


def test_explicit_music_question_reads_unchanged_current_track():
    layer=registry(); policy=PresenceContextPolicy()
    update(layer,'music',{'title':'A'},importance='important')
    block,selected=policy.build(layer,SCOPE,layer.snapshot(SCOPE),now=1000)
    policy.committed(SCOPE,selected,now=1000,chars=len(block))
    block,_=policy.build(layer,SCOPE,layer.snapshot(SCOPE),query='我在听什么音乐？',now=1001)
    assert '当前音乐：A' in block


def test_pause_does_not_include_old_title():
    layer=registry(); policy=PresenceContextPolicy()
    update(layer,'music',{'event_type':'music_paused'},importance='important')
    block,_=policy.build(layer,SCOPE,layer.snapshot(SCOPE),now=1000)
    assert '已暂停' in block
    assert '当前音乐：' not in block


def test_expiry_invalidates_old_music_once():
    layer=registry(); policy=PresenceContextPolicy()
    update(layer,'music',{'title':'A'},importance='important')
    block,selected=policy.build(layer,SCOPE,layer.snapshot(SCOPE),now=1000)
    policy.committed(SCOPE,selected,now=1000,chars=len(block))
    block,selected=policy.build(layer,SCOPE,layer.snapshot(SCOPE,now=1200),now=1200)
    assert '当前音乐无法确认' in block and '当前音乐：A' not in block
    policy.committed(SCOPE,selected,now=1200,chars=len(block))
    assert policy.build(layer,SCOPE,layer.snapshot(SCOPE,now=1201),now=1201)[0]==''


def test_initial_unknown_music_is_silent_unless_asked():
    layer=registry(); policy=PresenceContextPolicy()
    assert policy.build(layer,SCOPE,layer.snapshot(SCOPE),query='你好',now=1000)[0]==''
    assert '无法确认' in policy.build(layer,SCOPE,layer.snapshot(SCOPE),query='这首音乐',now=1000)[0]


def test_existing_body_not_duplicated_in_environment():
    layer=registry(); policy=PresenceContextPolicy()
    update(layer,'virtual.body',{'summary':'旧身体块'},importance='important')
    assert policy.build(layer,SCOPE,layer.snapshot(SCOPE),query='身体状态',now=1000)[0]==''


def test_complete_fragments_fit_budget_and_ordinary_repeats_stay_zero():
    layer=registry(); policy=PresenceContextPolicy()
    update(layer,'music',{'title':'A'*30},importance='important')
    update(layer,'device.battery',{'level':5},importance='critical')
    lengths=[]
    for i in range(6):
        block,selected=policy.build(layer,SCOPE,layer.snapshot(SCOPE,now=1000+i),query='继续聊天',now=1000+i,max_chars=420)
        lengths.append(len(block))
        if block:
            assert block.startswith(CONTEXT_START) and block.endswith(CONTEXT_END)
            policy.committed(SCOPE,selected,now=1000+i,chars=len(block))
    assert 0<lengths[0]<=420 and lengths[1:]==[0]*5


def test_duplicate_unknown_device_text_is_emitted_once():
    layer=registry(); policy=PresenceContextPolicy()
    layer.register('device.more',domains=('device.network','device.screen'))
    block,_=policy.build(layer,SCOPE,layer.snapshot(SCOPE),query='手机状态',now=1000)
    assert block.count('当前设备或活动信息无法确认。')==1


def test_cleanup_only_own_context_and_request_replacement():
    ordinary='正常文字'; block=CONTEXT_START+'内部背景'+CONTEXT_END
    req=SimpleNamespace(extra_user_content_parts=[SimpleNamespace(text=block),SimpleNamespace(text=ordinary)],system_prompt='人格')
    drop_request_context(req)
    assert [p.text for p in req.extra_user_content_parts]==[ordinary]
    assert req.system_prompt=='人格' and strip_context(ordinary+block)==ordinary


class Event:
    def __init__(self,user='peer',private=True,text='天气'):
        self.user=user; self.private=private; self.message_str=text
        self.unified_msg_origin='test:'+('FriendMessage' if private else 'GroupMessage')+':'+user
    def get_sender_id(self): return self.user
    def get_platform_name(self): return 'test'
    def get_self_id(self): return 'bot'
    def is_private_chat(self): return self.private


class Part:
    def __init__(self,text): self.text=text; self.temporary=False
    def mark_as_temp(self): self.temporary=True; return self


class Plugin:
    def __init__(self):
        self.cfg={}; self._stopping=False; self._space_data_dir=''
        self._last_signals={}; self._tick_health={}
    def _cfg_bool(self,key,default): return self.cfg.get(key,default)
    def _cfg_int(self,key,default): return self.cfg.get(key,default)
    def _resolve_session(self,user): return ('role_a',user) if user=='peer' else None
    def _tz(self): return 8


def host():
    plugin=Plugin(); context=SimpleNamespace(); integration=PresenceIntegration(plugin,context)
    integration.api._clock=lambda:1000
    return plugin,context,integration


def test_host_register_update_read_to_temporary_context_preserves_persona(monkeypatch):
    _,context,integration=host()
    monkeypatch.setattr('astrbot_plugin_virtual_body.core.presence_runtime.time.time',lambda:1000)
    req=SimpleNamespace(prompt='原始问题',system_prompt='原始人格',extra_user_content_parts=[])
    asyncio.run(integration.inject(Event(text='现在几点'),req,Part))
    assert context.presence_layer is integration.api
    assert req.prompt=='原始问题' and req.system_prompt=='原始人格'
    assert len(req.extra_user_content_parts)==1 and req.extra_user_content_parts[0].temporary
    assert '1970-01-01' in req.extra_user_content_parts[0].text


def test_host_group_and_unknown_peer_have_no_sensor_reads(monkeypatch):
    _,_,integration=host(); calls=[]
    async def collector(scope,now): calls.append(scope); return []
    integration.api.register('counter',domains=('test',),collector=collector)
    req=SimpleNamespace(extra_user_content_parts=[],system_prompt='人格')
    asyncio.run(integration.inject(Event(private=False),req,Part))
    asyncio.run(integration.inject(Event(user='stranger'),req,Part))
    assert calls==[] and req.extra_user_content_parts==[]


def test_host_close_clears_only_owned_interface():
    _,context,integration=host(); api=context.presence_layer
    integration.close()
    assert not hasattr(context,'presence_layer') and not api.active


def test_missing_temporary_channel_never_falls_back_to_persona():
    _,_,integration=host()
    req=SimpleNamespace(prompt='你好',system_prompt='人格',extra_user_content_parts=[])
    asyncio.run(integration.inject(Event(text='现在几点'),req,None))
    assert req.system_prompt=='人格' and req.prompt=='你好' and req.extra_user_content_parts==[]


def test_host_weather_uses_original_effective_observation_time(monkeypatch):
    plugin,_,integration=host(); plugin._space_data_dir='synthetic'
    monkeypatch.setattr('astrbot_plugin_virtual_body.core.presence_runtime.mood_signals.read_weather',
                        lambda *a,**kw:{'available':True,'city':'测试城','condition':'雨','is_rain':True,'observed_at':950})
    rows=asyncio.run(integration._collect_environment(SCOPE,1000))
    weather=next(r for r in rows if r['domain']=='weather')
    assert weather['observed_at']==950 and weather['value']['location_scope']=='last_weather_query'


def test_host_scope_changed_during_collect_does_not_commit(monkeypatch):
    plugin,_,integration=host()
    monkeypatch.setattr('astrbot_plugin_virtual_body.core.presence_runtime.time.time',lambda:1000)
    async def collector(scope,now):
        plugin._resolve_session=lambda user:None
        return [{'domain':'music','value':{'title':'不得提交'},'observed_at':1000,'importance':'important'}]
    integration.api.register('pending',domains=('music',),collector=collector,
                             renderer=lambda *args:'不得提交')
    req=SimpleNamespace(extra_user_content_parts=[],system_prompt='人格')
    asyncio.run(integration.inject(Event(),req,Part))
    assert req.extra_user_content_parts==[] and integration.policy.metrics['injections']==0
