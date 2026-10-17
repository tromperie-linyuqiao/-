# Presence API v1

本插件提供可选的 `context.presence_layer`。其他插件只依赖公开方法，不导入本插件、其他感知插件或它们的数据文件。本说明描述实际接口，不包含尚未发行的手机、音乐、书柜或陪伴适配器。

## 服务发现与来源生命周期

```python
api = getattr(context, "presence_layer", None)
if api is not None and api.api_version == 1 and api.active:
    owner = self
    accepted = api.register(
        "example.weather",
        owner=owner,
        domains=("weather",),
        renderer=render_weather,
        priority=50,
    )
```

来源名和域名使用短的字母、数字、下划线、点、冒号或连字符标识，名称以字母、数字或下划线开头。每个插件使用自己的命名空间。同名 `register` 会替换来源并清除旧缓存，因此不要每条消息无条件重新注册。只在首次绑定、新 API 实例出现或来源配置确实需要替换时注册。

卸载来源时调用 `api.unregister("example.weather", owner=self)`。旧实例不能用不同 owner 撤销新实例的注册。宿主重载后 API 对象会变化，来源需要发现新对象并重新注册；旧对象不再接收有效更新。

接入方应缓存已绑定的 API 对象身份。收到事件时重新发现服务：不存在、`api_version != 1` 或 `active` 为 false 就跳过；只有对象改变或来源设置确需替换时才重新 `register`，并检查返回值。`register` 被拒绝时不要继续 `update`；`update` 为 false 时记录不含用户数据的来源诊断，不把它当作发布成功。自己卸载时针对已经绑定的对象 `unregister(..., owner=self)`，不能依赖下一次聊天帮忙清理。

这是受信插件之间的进程内契约，owner 防止旧实例误操作，不是恶意插件沙箱，也不是可直接公开的匿名 HTTP 写入接口。

## 身份作用域

```python
scope = api.scope_for_event(event)
```

scope 包含 `platform`、`bot_id`、`user_id`、`character_id`、`session_id`、`private`。未知字段保留 null。宿主自动注入还会检查真实私聊、角色白名单和当前角色绑定；来源插件仍须验证自己用户与设备的所有权，不能认为传入任意 scope 就获得感知权限。

缓存按以上身份维度隔离，设备另由记录的 `device_id` 区分。不要从接收者 ID 伪造 bot_id，也不要把群聊当成私聊。

需要主动生成时，可使用 `api.scope_for_session(umo)` 获取由真实私聊登记的身份租约。无租约或角色授权已改变时应跳过环境背景，不猜账号。通用接入无需依赖任何专有陪伴事件标记。

## 发布和读取观测

```python
def publish_weather(api, event, *, condition, observed_at):
    scope = api.scope_for_event(event)
    return api.update(
        "example.weather", scope, "weather",
        {"condition": condition},
        observed_at=observed_at,
        ttl_seconds=600,
        evidence_kind="observed",
    )

snapshot = api.read(scope, "weather")
record = snapshot["states"]["weather"]
```

`update` 返回是否接受，不表示事实一定改变。返回值为可 JSON 序列化的副本，修改 snapshot 不影响缓存。`read(scope)` / `snapshot(scope)` 读取全部已注册域，`read(scope, domain)` 可过滤单域，`source_for(scope, domain)` 返回当前选中来源名。

### 记录结构

```json
{
  "value": {"condition": "小雨"},
  "source": "example.weather",
  "status": "fresh",
  "meta": {
    "observed_at": "2026-01-01T12:00:00Z",
    "received_at": "2026-01-01T12:00:01Z",
    "expires_at": "2026-01-01T12:10:00Z",
    "source_revision": null,
    "evidence_kind": "observed",
    "device_id": null,
    "importance": "normal",
    "change_key": {"condition": "小雨"}
  }
}
```

示例时间仅表示结构。真正发布时必须使用来源的观测时刻，不能照搬示例或在轮询缓存时改成现在。

| 字段或参数 | 契约 |
| --- | --- |
| `status` | `fresh`、`unknown`、`stale`、`unavailable`、`disabled`、`ambiguous`、`conflict` |
| `observed_at` | 有时区的 ISO 时间或有限 Unix 秒时间戳。`fresh` 且省略或传 null 时按接收时刻记录；重放缓存必须保留原观测时刻。没有观测时使用明确的 `unknown` / `unavailable`，并传 null |
| `received_at` | 可选接收时间，默认宿主当前时间；不替代源观测时间 |
| `ttl_seconds` | 0–604800 秒，fresh 到期后读取为 stale；读取不续期 |
| `evidence_kind` | `observed`、`reported`、`inferred`、`simulated`、`derived`，应区分设备实测和用户自报 |
| `importance` | `normal`、`important`、`critical`；重要背景才按变化自动进入普通闲聊 |
| `revision` | 可选源顺序版本，映射到 `source_revision`；禁止 dict/list/bool |
| `change_key` | 可选语义去重值；缺省等于 value，可移除进度等非事实变化 |
| `device_id` | 可选来源设备 ID；不能用它代替用户授权 |

单值最多 32 KiB UTF-8 JSON、12 层嵌套；拒绝循环、非有限数字、非 JSON 类型和超限数据。明显未来时间会被拒绝，不能依赖未来帧覆盖当前事实。服务最多 32 来源、每来源 32 域、256 个缓存 scope 和 1024 个记录；达到上限会拒绝新来源或回收旧缓存，不是永久数据库。

`received_at` 默认当前接收时刻。如果只是在轮询旧快照，不能用 `status="fresh", observed_at=None` 再发布，否则会被当成刚接收到的新观测而延长有效期。没有可用观测时可发布 `value=None, status="unknown", observed_at=None`；已知来源不可用时用 `status="unavailable"`。新鲜、未知和不可用须由来源诚实区分。

同优先级的 fresh 事实互相矛盾时返回 conflict，不擅自选一个当真。显式高优先级可覆盖较低优先级来源。snapshot 的 `revision` 跟随语义事实或可用性变化，而不是每次传输时间刷新。

## Collector 与 Renderer

```python
async def collect_weather(scope, now):
    # 接入方从自己的已授权来源读取，保留该来源的 observed_at。
    observation = await read_own_weather_observation(scope)
    if observation is None:
        return [{"domain": "weather", "value": None, "status": "unknown"}]
    return [{
        "domain": "weather",
        "value": {"condition": observation["condition"]},
        "observed_at": observation["observed_at"],
        "ttl_seconds": 600,
        "status": "fresh",
        "evidence_kind": "observed",
    }]

def render_weather(domain, record, scope, query, max_chars, now):
    if record["status"] != "fresh" or not isinstance(record["value"], dict):
        return ""
    condition = " ".join(str(record["value"].get("condition", "")).split())[:80]
    text = "最近天气观测：" + condition + "；观测地点不自动代表用户当前位置。"
    return text if condition and len(text) <= max_chars else ""

api.register("example.weather", owner=self, domains=("weather",),
             collector=collect_weather, renderer=render_weather,
             enabled=lambda: self.sensor_enabled, priority=50)
await api.refresh(scope, timeout_seconds=2)
```

`read_own_weather_observation` 是接入方应实现的本地函数占位，不是本插件或 AstrBot 提供的接口；示例没有虚构可用的天气服务。可以完全不使用 collector，改为收到观测后直接 `update`。

collector 支持异步函数或同步快照函数，返回最多 64 项的 list/tuple。同 scope 同时 refresh 共享在途采集；来源异常、超时、替换、关闭或宿主卸载不会让迟到结果回填新一代状态。同步 collector 会在线程中执行，宜只返回快照，不直接跨线程改 API 内部数据。

返回的是来源对该 scope 和域的当前选定设备集合，成功批次会撤去该域未选中的旧设备。无新事实或来源不可用时请返回明确 unknown/unavailable 记录；空数组不会把旧歌曲变成暂停，也不会延长旧记录有效期。来源必须遵守协程取消；Python 不能安全强杀吞取消的协程或已进入系统调用的线程。

renderer 保持简短、去除不可信控制内容、返回完整片段，并严格遵守 `max_chars`。状态文本是资料，不应当作系统指令。新自定义域可以用 `importance=important` 参与变化注入，或通过查询工具指定域名读取；不保证每个新域都自动获得自然语言主题匹配词。

## 音乐数据与停止水位

建议音乐来源发布标准结构：

```json
{
  "event_type": "music_playing",
  "song_id": "example-track",
  "song_info": {"title": "合成示例曲", "artist": "合成演奏者"},
  "user_state": {"playing": true}
}
```

域名使用 `music`，当前曲目变化标记 important。暂停用 `music_paused` / `music_stopped` 或明确 `playing=false`，不可用时使用 unknown/unavailable 且 `playing=null`。暂停后较旧或相同水位的播放重传不会让旧曲复活。一个数值递增的源 revision 可进一步排序同时间戳事件。

`change_key` 推荐只含播放状态、曲目身份和可靠性等语义字段，排除 progress、年龄和累计播放时长。原 value 仍可以保留最新进度。音乐内容只作背景，不据曲名推断用户情绪，不把整份曲库或歌词注入。

## 上下文与按需查询

```python
await api.refresh(scope, timeout_seconds=2)
text = api.render_context(scope, query="天气怎么样？", max_chars=420, channel="my_sender")
```

`render_context` 返回选定的完整环境块；它本身不发送消息、不请求模型。普通状态按相关问题读取，重要状态按变化与重述间隔注入，初次未知状态通常保持静默。原身体/空间块不在环境块中重复。字符预算不是计费 token。

显式调用 `render_context` 会消费该 channel 的一次背景投影；调用方应先确认自己的 Provider 能接收并保留临时内容语义，再把返回值放入 `TextPart(text=text).mark_as_temp()`。不要拼进原用户文本、人格模板、长期记忆或聊天历史。通用查询工具 `presence_context_query` 可用 `aspect="weather"` 或具体域名按需取证。

普通 AstrBot 请求的本插件钩子已处理同 req 重试：只保留一个自己的环境片段，按最新状态重建，过期/停播/撤权后不复用旧文字；真正附加成功后才提交去重。不同新消息仍沿用背景去重策略。

配置项为 `presence_layer_enabled`、`presence_context_enabled`、`presence_context_max_chars`（160–1200，默认 420）、`presence_repeat_seconds`（30–3600，默认 300）。越界数值回默认值，诊断显示有效值。关闭自动环境背景仍可保留按需查询，但来源的注册与降级由接入方自己负责。

## 发行范围和维护边界

本候选自带时间和模拟身体/空间状态；默认不会读取其他插件的私人数据库。外部音乐、天气、设备、阅读等必须通过标准接口提供，缺失时保持未知。内置详细天气查询、多源预警图片、手机 APK、私有曲库和专有陪伴出口均不随本候选发行。

唯一保留的旧目录兼容操作是显式的 `sp_migrate_legacy_manor`（默认 false）：开启后才在初始化期间读取旧庄园空间/物品文件并写 `.migrated` 标记，需事先备份并确认数据使用权限；不删除旧文件、不覆盖已有目标。此操作不提供感知数据，也不属于 provider 自动发现。

此 API 的观测读取与旧心绪引擎的规则信号不是自动双向映射。外部天气等 provider 可以提供当前 Context，但不能据此宣称旧雨天动线或所有心绪规则已经随它联动；新的行为消费方需要独立实现并测试。

`clear` / `close` 是宿主维护操作，会失效缓存、在途结果和真实会话租约，不应用来伪造新鲜度。缓存是内存投影；原角色参数、身体/空间与规则覆盖层由本插件运行数据目录持久化，更新候选时不得把运行数据一起上传。

主体采用 [AGPL-3.0-or-later](LICENSE)，第三方声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；许可决策沿革见 [LICENSE_PENDING.md](LICENSE_PENDING.md)。
