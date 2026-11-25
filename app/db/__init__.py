"""SQLite storage: AI runs, proposals, signals, news, equity, protective orders,
ML models, LLM usage and the activity log.

Package layout:
  connection.py   connect() + PRAGMAs, LockedConnection (one RLock around every statement)
  schema.py       SCHEMA (baseline), ADDED_COLUMNS, decode(), drift helpers
  migrations/     numbered migrations tracked by PRAGMA user_version
  <domain>.py     repository mixins (proposals, ledger, signals, lessons, ml, events, kv, ...)

``DB`` is the facade that mixes the repositories together, so every method name and
return shape of the old single-file ``app/db.py`` is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from .atm_iv import AtmIvRepo
from .option_spreads import OptionSpreadsRepo
from .option_outcomes import OptionOutcomesRepo
from .autopsies import AutopsiesRepo
from .base import BaseRepo
from .connection import LockedConnection, connect
from .equity import EquityRepo
from .events import EventsRepo
from .kv import KvRepo
from .ledger import LedgerRepo
from .lessons import LessonsRepo
from .maintenance import MaintenanceRepo
from .notify import NotifyMixin
from .migrations import current_version, latest_version, run_migrations
from .ml import MLRepo
from .proposals import DuplicateProposalError, ProposalsRepo
from .protective import ProtectiveRepo
from .research import ResearchRepo
from .reviews import ReviewsRepo
from .runs import RunsRepo
from .schema import ADDED_COLUMNS, JSON_FIELDS, SCHEMA, _now, decode
from .signals import SignalsRepo
from .shadows import ShadowsRepo
from .sleeves import SleevesRepo

__all__ = [
    "ADDED_COLUMNS", "DB", "DuplicateProposalError", "JSON_FIELDS", "LockedConnection", "SCHEMA",
    "_now", "connect", "current_version", "decode", "latest_version", "run_migrations",
]


class DB(EventsRepo, KvRepo, RunsRepo, ProposalsRepo, SignalsRepo, EquityRepo, ProtectiveRepo,
         MLRepo, ReviewsRepo, ResearchRepo, LedgerRepo, AutopsiesRepo, LessonsRepo, AtmIvRepo, OptionSpreadsRepo,
         OptionOutcomesRepo, SleevesRepo, ShadowsRepo,
         MaintenanceRepo, NotifyMixin, BaseRepo):
    def __init__(self, path: Path, broker_mode: str = "mock",
                 webull_environment: str | None = None, account_id: str | None = None) -> None:
        self.path = Path(path)
        self.conn = connect(self.path)
        # Called as fn(level, message, alert) for every log line (see alerts.py).
        self.listeners: list = []
        # Migrations run here, in the constructor, so they always finish before the first
        # write (see app/db/migrations).
        run_migrations(self.conn)
        # Namespace used to filter every query
        self.broker_mode = broker_mode
        self.webull_environment = webull_environment
        self.account_id = account_id
