"""Orchestration: signals -> proposals -> consensus gate -> risk checks -> preview -> order.

``app/engine.py`` became this package. ``Engine`` is composed from mixins, one per concern:

  core.py         init, lessons auto-apply, mode, kill switch
  cycle.py        the slow deterministic cycle (cold-path refresh outside ``_run_lock``)
  proposals.py    proposal generation and validation (``_on_proposal`` runs under a lock)
  options_flow.py fail-closed option gate, true IV rank, daily ATM IV history
  execution.py    execute / requote / _send (brackets, stops, options)
  reconcile.py    unknown-order reconcile and ``startup_reconcile``
  flatten.py      emergency cancel / flatten
  rebalance.py    core-satellite rebalancing
  scheduler.py    the slow-cycle scheduler
  option_exits.py OptionExitManager (H1)
  risk_loop.py    RiskLoop (sub-second exits)
  workers.py      cold-path workers feeding ``SignalStore``

Lifecycle (called by the API lifespan, in this order; shutdown in reverse):

    await engine.startup_reconcile()
    await engine.start_workers()
    await engine.start_risk_loop()
    engine.start_scheduler()                  # as before
    ...
    await engine.stop_scheduler(); await engine.stop_risk_loop(); await engine.stop_workers()

Events published on ``app.eventhub.hub``:
    kill       {"on": bool, "reason": str|None, "at": ISO8601}          every kill change
    proposals  [proposal row, ...] (same shape as GET /api/proposals)   created/changed (via DB)
    events     [event row, ...] (same shape as GET /api/events)         every ``db.log`` (via DB)
    fill       {"proposal_id", "symbol", "side", "quantity", "price",
                "client_order_id", "at"}                                fills seen by reconcile
"""

from __future__ import annotations

# Names the old single-file module exposed (tests and other modules import them from here).
from .. import consensus, exits, ta  # noqa: F401
from ..broker import Broker, BrokerError  # noqa: F401
from ..config import Settings, estimate_cost  # noqa: F401
from ..core import (calculate_core_state, check_satellite_drawdown_guard,  # noqa: F401
                    create_rebalance_plan, format_core_satellite_report, satellite_headroom_usd)
from ..db import DB, decode  # noqa: F401
from ..eligibility import check_eligibility  # noqa: F401
from ..market import (market_is_open, market_is_open_extended, now_utc,  # noqa: F401
                      order_session, session_phase)
from ..portfolio import ACTIVE, DONE_CANCELLED, parse_orders  # noqa: F401
from ..risk import OptionRiskManager, RiskManager  # noqa: F401
from ..signals import SignalService  # noqa: F401
from ..sizing import max_allowed_qty, ml_size_factor, volatility_scaled_qty  # noqa: F401
from .core import AUTO_APPLICABLE, NEVER_AUTO_APPLY, CoreMixin
from .cycle import CycleMixin
from .execution import ExecutionMixin
from .flatten import FlattenMixin
from .option_exits import HeldOption, OptionExitManager, evaluate_exit, parse_held_options
from .options_flow import OptionsFlowMixin, iv_rank, normalize_iv
from .proposals import ProposalsMixin
from .rebalance import RebalanceMixin
from .reconcile import ReconcileMixin
from .risk_loop import RiskLoop, RiskLoopMixin
from .scheduler import SchedulerMixin
from .util import (MAX_PROPOSALS_PER_RUN, _coid, _is_option_symbol, _session_rejection,  # noqa: F401
                   normalize_proposal, parse_occ)
from .workers import Workers, WorkersMixin

# Every status the engine/DB writes to ``proposals.status`` (the exact set):
#   pending     waiting for approval (approve mode) or for the end of the run (auto mode)
#   submitting  claimed; the order is being sent
#   placed      accepted by the broker (working or filled; fills are tracked in the ledger)
#   rejected    refused by the gate, the risk checks or at execution
#   dismissed   dismissed by the user, by the kill switch, or superseded by an exit order
#   failed      the broker refused it, or it was never sent
#   unknown     no reply from the broker; settled by ``reconcile_orders`` via client_order_id
#   orphan      an open broker order this app had no record of (adopted at startup, never executed)
PROPOSAL_STATUSES = frozenset({
    "pending", "submitting", "placed", "rejected", "dismissed", "failed", "unknown", "orphan",
})


class Engine(CoreMixin, CycleMixin, ProposalsMixin, OptionsFlowMixin, ExecutionMixin,
             ReconcileMixin, FlattenMixin, RebalanceMixin, SchedulerMixin, RiskLoopMixin,
             WorkersMixin):
    """The trading engine. See the module docstring for the lifecycle and the events."""


TradingEngine = Engine

__all__ = [
    "AUTO_APPLICABLE", "Engine", "HeldOption", "MAX_PROPOSALS_PER_RUN", "NEVER_AUTO_APPLY",
    "OptionExitManager", "PROPOSAL_STATUSES", "RiskLoop", "TradingEngine", "Workers",
    "_coid", "_is_option_symbol", "_session_rejection", "evaluate_exit", "iv_rank",
    "normalize_iv", "normalize_proposal", "parse_held_options", "parse_occ",
]
