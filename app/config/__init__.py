"""App settings, loaded from the .env file in the project root.

Package layout: ``env`` (paths, helpers, env-var registry), ``settings`` (the flat
``Settings`` dataclass), ``groups`` (grouped read-only views), ``loader``
(``load_settings``) and ``pricing``. Every name the old ``app/config.py`` exported is
re-exported here.
"""

from .env import (DATA_DIR, LESSONS_FILE, LOG_DIR, MODEL_DIR, REGISTRY, ROOT, STRATEGY_FILE,
                  WEB_DIR, _bool, _build_watchlist, _float, _int, _raw, _sector_map, _str,
                  _symbols)
from .groups import (AlertSettings, BacktestSettings, FeedSettings, LLMSettings, MLSettings,
                     OptionsSettings, RiskSettings)
from .loader import load_settings
from .pricing import PRICING, estimate_cost
from .settings import Settings

__all__ = [
    "AlertSettings", "BacktestSettings", "DATA_DIR", "FeedSettings", "LESSONS_FILE", "LLMSettings",
    "LOG_DIR", "MLSettings", "MODEL_DIR", "OptionsSettings", "PRICING", "REGISTRY", "ROOT",
    "RiskSettings", "STRATEGY_FILE", "Settings", "WEB_DIR", "estimate_cost", "load_settings",
]
