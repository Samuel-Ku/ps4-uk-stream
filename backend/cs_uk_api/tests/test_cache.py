import time

from cs_uk_api.cache import TtlCache


def test_ttl_cache_returns_value_within_ttl():
    c = TtlCache(default_ttl_s=1)
    c.set("k", "v1")
    assert c.get("k") == "v1"


def test_ttl_cache_expires_value():
    c = TtlCache(default_ttl_s=0)
    c.set("k", "v1")
    time.sleep(0.01)
    assert c.get("k") is None


def test_ttl_cache_uses_call_specific_ttl():
    c = TtlCache(default_ttl_s=60)
    c.set("k", "v", ttl_s=0)
    time.sleep(0.01)
    assert c.get("k") is None
