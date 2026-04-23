"""Unit tests for db.settings_cache — short-TTL DB-backed flag reader.

Covers: bool/int/float coercion, default fallback, cache TTL, cache
invalidation, and strategy-scoped vs global resolution.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest


class TestSettingsCache:
    def setup_method(self):
        from db import settings_cache
        settings_cache.invalidate()

    def test_get_bool_returns_default_when_row_missing(self):
        from db import settings_cache
        with patch.object(settings_cache, "_fetch_raw", return_value=None):
            assert settings_cache.get_bool("MISSING_KEY", default=False) is False
            assert settings_cache.get_bool("MISSING_KEY", default=True) is True

    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("TRUE", True), ("True", True),
        ("1", True), ("yes", True), ("on", True), ("t", True), ("y", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
        ("anything-else", False),
    ])
    def test_get_bool_parses_common_forms(self, raw, expected):
        from db import settings_cache
        settings_cache.invalidate()
        with patch.object(settings_cache, "_fetch_raw", return_value=raw):
            assert settings_cache.get_bool("K") is expected

    def test_get_int_coerces(self):
        from db import settings_cache
        with patch.object(settings_cache, "_fetch_raw", return_value="42"):
            assert settings_cache.get_int("K") == 42

    def test_get_int_falls_back_on_bad_value(self):
        from db import settings_cache
        with patch.object(settings_cache, "_fetch_raw", return_value="abc"):
            assert settings_cache.get_int("K", default=7) == 7

    def test_get_float_coerces(self):
        from db import settings_cache
        with patch.object(settings_cache, "_fetch_raw", return_value="3.14"):
            assert settings_cache.get_float("K") == pytest.approx(3.14)

    def test_ttl_caches_repeated_reads(self):
        from db import settings_cache
        settings_cache.invalidate()
        call_count = {"n": 0}

        def _fake(key, strategy_id):
            call_count["n"] += 1
            return "true"

        with patch.object(settings_cache, "_fetch_raw", side_effect=_fake):
            for _ in range(50):
                assert settings_cache.get_bool("K") is True
        assert call_count["n"] == 1, (
            f"settings_cache should cache within TTL — hit DB "
            f"{call_count['n']} times instead of 1"
        )

    def test_cache_invalidate_forces_refresh(self):
        from db import settings_cache
        settings_cache.invalidate()
        call_count = {"n": 0}

        def _fake(key, strategy_id):
            call_count["n"] += 1
            return "true"

        with patch.object(settings_cache, "_fetch_raw", side_effect=_fake):
            settings_cache.get_bool("K")
            settings_cache.invalidate()
            settings_cache.get_bool("K")
        assert call_count["n"] == 2

    def test_strategy_scoped_key_separate_from_global(self):
        """A global (strategy_id=None) and scoped (strategy_id=91) lookup
        should NOT share a cache slot."""
        from db import settings_cache
        settings_cache.invalidate()

        def _fake(key, strategy_id):
            return "true" if strategy_id is None else "false"

        with patch.object(settings_cache, "_fetch_raw", side_effect=_fake):
            assert settings_cache.get_bool("K") is True                 # global
            assert settings_cache.get_bool("K", strategy_id=91) is False
