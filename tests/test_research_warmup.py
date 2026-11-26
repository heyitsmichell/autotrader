import asyncio

import pandas as pd

from app import research_views as rv


def test_warm_cache_populates_cache(monkeypatch):
    rv.clear_cache()
    df = pd.DataFrame({"A": [1.0, 2.0]})
    monkeypatch.setattr(rv, "load_sp500_closes", lambda: df)
    monkeypatch.setattr(rv, "load_market_closes", lambda: df)
    monkeypatch.setattr(rv, "load_vaa_closes", lambda: df)
    asyncio.run(rv.warm_cache(delay=0))
    assert set(rv._CACHE) == {"sp500", "market", "vaa"}
    rv.clear_cache()


def test_warm_cache_swallows_exceptions(monkeypatch):
    rv.clear_cache()

    def boom():
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(rv, "load_sp500_closes", boom)
    monkeypatch.setattr(rv, "load_market_closes", boom)
    monkeypatch.setattr(rv, "load_vaa_closes", boom)
    asyncio.run(rv.warm_cache(delay=0))   # must not raise
    assert not rv._CACHE

    async def bad_closes(name):
        raise ValueError("x")

    monkeypatch.setattr(rv, "closes", bad_closes)
    asyncio.run(rv.warm_cache(delay=0))   # outer guard also swallows
