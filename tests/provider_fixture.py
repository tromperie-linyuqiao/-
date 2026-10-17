"""Synthetic providers exercising the actual public Presence contract."""
from astrbot_plugin_virtual_body.core.presence import PresenceLayer

SCOPE = {"platform": "test", "bot_id": "demo-bot", "user_id": "demo-user",
         "character_id": "example", "session_id": "test:FriendMessage:demo-user", "private": True}


def provider(domain, value, *, now=1000.0, observed_at=None, ttl=600):
    api = PresenceLayer(clock=lambda: now)
    assert api.register("synthetic", domains=(domain,))
    assert api.update("synthetic", SCOPE, domain, value,
                      observed_at=now if observed_at is None else observed_at,
                      ttl_seconds=ttl, evidence_kind="reported")
    return api


def signal(api, domain, now=1000.0, scope=None):
    """Test-side rule input projection; never a new production API claim."""
    record = api.read(SCOPE if scope is None else scope, domain, now=now)["states"].get(domain, {})
    available = record.get("status") == "fresh"
    return {**(record.get("value") if available and isinstance(record.get("value"), dict) else {}),
            "available": available, "stale": record.get("status") == "stale"}
