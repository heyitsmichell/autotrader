"""Settings groups: exhaustive, non-overlapping, values equal the flat fields."""
import dataclasses
from collections import Counter

import pytest

from app.config import Settings
from app.config.groups import GROUP_FIELDS

_ATTR = {"RiskSettings": "risk", "OptionsSettings": "options", "MLSettings": "ml",
         "FeedSettings": "feed", "AlertSettings": "alerts", "BacktestSettings": "backtest",
         "LLMSettings": "llm", "SleevesSettings": "sleeves", "NewsSettings": "news",
         "BrokerSettings": "broker", "OrderSettings": "orders", "CoreSettings": "core"}


def test_every_field_in_exactly_one_group():
    counts = Counter(n for names in GROUP_FIELDS.values() for n in names)
    flat = {f.name for f in dataclasses.fields(Settings)}
    assert set(counts) == flat
    assert [n for n, c in counts.items() if c != 1] == []


def test_group_attr_map_complete():
    assert set(_ATTR) == set(GROUP_FIELDS)


@pytest.mark.parametrize("cls_name", sorted(_ATTR))
def test_group_values_equal_flat(test_settings, cls_name):
    s = test_settings
    g = getattr(s, _ATTR[cls_name])
    assert type(g).__name__ == cls_name
    assert [f.name for f in dataclasses.fields(g)] == list(GROUP_FIELDS[cls_name])
    for n in GROUP_FIELDS[cls_name]:
        assert getattr(g, n) == getattr(s, n)


def test_replace_reflected_and_frozen(test_settings):
    s2 = dataclasses.replace(test_settings, max_order_usd=7.0, shadows_enabled=True)
    assert s2.risk.max_order_usd == 7.0 and s2.core.shadow_start_capital == s2.shadow_start_capital
    assert s2.sleeves.shadows_enabled is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        s2.core.groq_api_key = "x"  # type: ignore[misc]
