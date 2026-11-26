"""Self-learning loop, scored only on what happened after each proposal.

1. Outcomes (no AI, free): for every proposal (placed, rejected or dismissed),
   record the price move 1 and 5 trading days later in the proposal's direction,
   plus SPY over the same 5 days as a benchmark.
2. Review (one LLM call): the model reads the scorecard and writes a DRAFT of lessons. The
   output is schema-validated and length-capped, stored in the reviews table, and written
   only to ``lessons.proposed.md`` (a draft nobody reads automatically). It never touches
   lessons.md and never changes a setting; any parameter suggestion becomes a lesson in
   status 'proposed' that a human must approve (and the engine still needs n >= 30 and
   p < 0.05 to apply it, so LLM text alone can never steer trading). This is the
   "reflection on past decisions" idea from FinMem / FinAgent, applied to live results only.
3. Nightly job (after market close): evaluates outcomes, runs review, applies lessons,
   retrains ML, proposes parameter changes from autopsies.

Why forward-only: LLMs memorize market history, so replaying past dates
(a backtest) lets the model "remember" outcomes. Lessons can only change what
the model proposes. The risk limits in code are never touched.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from .broker import Broker, BrokerError, bar_day, parse_bars
from .config import LESSONS_FILE, STRATEGY_FILE, Settings, estimate_cost
from .db import DB
from .engine import Engine
from .llm import chat_completion, parse_json_response, provider_model, usage_counts
from .market import ET, market_is_open, now_et, now_utc, session_close
from .ml import MLFilter
from .lessons import propose_lessons_from_autopsies, validate_learnable_param
from .sentiment import sanitize_headline

log = logging.getLogger(__name__)
BENCHMARK = "SPY"

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {
            "type": "string",
            "description": "Markdown bullet list, at most 8 bullets, under 250 words",
        },
        "strategy_suggestions": {
            "type": "string",
            "description": "Optional suggested edits to strategy.md for the owner to consider; "
                           "empty string if none",
        },
        "evidence_strength": {"type": "string", "enum": ["weak", "moderate", "strong"]},
    },
    "required": ["lessons", "strategy_suggestions", "evidence_strength"],
    "additionalProperties": False,
}

REVIEW_SYSTEM = """\
You review the track record of an AI stock-trading analyst and write the lessons \
it will read before every future run.

You get the owner's strategy, the current lessons, summary statistics, and recent \
proposals. Each proposal shows the indicator values when it was made and its \
outcome: the % move 1 and 5 trading days later in the proposal's direction \
(positive means the call was right), and SPY's 5-day move for comparison. \
Rejected and dismissed proposals were never traded; their outcomes show what \
would have happened.

Rules:
- Small samples are mostly noise. Every lesson must cite its sample size, e.g. \
"BUYs with RSI14 above 70: avg -1.8% over 5 days (n=9)". With fewer than about \
30 outcomes, keep lessons few and tentative. When the data supports no change, \
say so.
- A strategy that doesn't beat simply holding SPY isn't adding value. Say so \
plainly if that's the case.
- Lessons must be concrete and checkable against indicator values. No generic \
advice like "be careful".
- Keep earlier lessons only if the data still supports them.
- Risk limits are fixed by the owner and out of scope. Never suggest loosening \
them or trading more often.
"""


EVIDENCE_LEVELS = ("weak", "moderate", "strong")
MAX_LESSONS_CHARS = 3000
MAX_SUGGESTIONS_CHARS = 2000
MAX_BULLETS = 8
MAX_PARAM_SUGGESTIONS = 5


def proposed_lessons_path():
    """Where LLM-written lesson drafts go. Deliberately not LESSONS_FILE."""
    return LESSONS_FILE.with_name("lessons.proposed.md")


def _clean_text(v, limit: int) -> str:
    """Strip control characters and cap the length (LLM output is untrusted text)."""
    t = "".join(ch for ch in str(v) if ch in "\n\t" or ch.isprintable())
    return t.strip()[:limit]


def validate_review(out) -> tuple[dict | None, list[str]]:
    """Schema-validate the LLM review. Returns (clean_output | None, reasons).

    Required: ``lessons`` (str), ``strategy_suggestions`` (str), ``evidence_strength`` (one of
    weak/moderate/strong). Text is control-char stripped and capped, lessons to MAX_BULLETS
    lines. Optional ``parameter_suggestions`` items ({param_name, value, rationale}) are each
    checked against the learnable-parameter registry and its bounds (risk limits and unknown
    names are refused); invalid items are skipped with a reason. ``None`` means the whole review
    is unusable (missing or mistyped required keys).
    """
    reasons: list[str] = []
    if not isinstance(out, dict):
        return None, [f"review is not a JSON object (got {type(out).__name__})"]
    for key in ("lessons", "strategy_suggestions", "evidence_strength"):
        if key not in out:
            reasons.append(f"missing required key '{key}'")
        elif not isinstance(out[key], str):
            reasons.append(f"'{key}' must be a string, got {type(out[key]).__name__}")
    if reasons:
        return None, reasons
    if out["evidence_strength"] not in EVIDENCE_LEVELS:
        return None, [f"evidence_strength {out['evidence_strength']!r} not in {EVIDENCE_LEVELS}"]

    lessons = _clean_text(out["lessons"], MAX_LESSONS_CHARS)
    if not lessons:
        return None, ["'lessons' is empty"]
    lines = [ln for ln in lessons.splitlines() if ln.strip()]
    if len(lines) > MAX_BULLETS:
        reasons.append(f"lessons had {len(lines)} lines; kept the first {MAX_BULLETS}")
        lessons = "\n".join(lines[:MAX_BULLETS])
    clean: dict = {
        "lessons": lessons,
        "strategy_suggestions": _clean_text(out["strategy_suggestions"], MAX_SUGGESTIONS_CHARS),
        "evidence_strength": out["evidence_strength"],
        "parameter_suggestions": [],
    }

    raw = out.get("parameter_suggestions") or []
    if not isinstance(raw, list):
        reasons.append("parameter_suggestions ignored: not a list")
        raw = []
    for i, item in enumerate(raw):
        if len(clean["parameter_suggestions"]) >= MAX_PARAM_SUGGESTIONS:
            reasons.append(f"parameter_suggestions[{i}:] skipped: more than {MAX_PARAM_SUGGESTIONS}")
            break
        if not isinstance(item, dict):
            reasons.append(f"parameter_suggestions[{i}] skipped: not an object")
            continue
        name, value = item.get("param_name"), item.get("value")
        if not isinstance(name, str):
            reasons.append(f"parameter_suggestions[{i}] skipped: param_name must be a string")
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or value != value or value in (float("inf"), float("-inf"))):
            reasons.append(f"parameter_suggestions[{i}] ({name}) skipped: value must be a finite number")
            continue
        ok, why = validate_learnable_param(name, float(value))
        if not ok:
            reasons.append(f"parameter_suggestions[{i}] ({name}={value}) skipped: {why}")
            continue
        clean["parameter_suggestions"].append({
            "param_name": name, "value": float(value),
            "rationale": _clean_text(item.get("rationale", ""), 500)})
    return clean, reasons


def _entry_day(created_at: str):
    return datetime.fromisoformat(created_at).astimezone(ET).date()


def _fmt(v, spec="{:+.2f}") -> str:
    return "n/a" if v is None else spec.format(v)


def _bins(items: list[dict], key: str, edges: list[float], ret: str = "ret_5d") -> list[dict]:
    """Hit rate and average return per bucket of `key` (for calibration charts)."""
    out = []
    for lo, hi in zip(edges, edges[1:]):
        rows = [x for x in items if x.get(key) is not None and lo <= x[key] < hi]
        r = [x[ret] for x in rows]
        out.append({"lo": lo, "hi": hi, "n": len(r),
                    "hit_rate": round(sum(v > 0 for v in r) / len(r) * 100, 1) if r else None,
                    "avg": round(sum(r) / len(r), 3) if r else None})
    return out


def _summary(items: list[dict], ret: str = "ret_5d") -> dict:
    r = [x[ret] for x in items if x.get(ret) is not None]
    return {
        "n": len(r),
        "hit_rate": round(sum(v > 0 for v in r) / len(r) * 100, 1) if r else None,
        "avg_5d": round(sum(r) / len(r), 3) if r else None,
    }


class Learner:
    def __init__(self, settings: Settings, broker: Broker, db: DB, ml=None, research=None) -> None:
        self.s, self.broker, self.db, self.ml, self.research = settings, broker, db, ml, research
        self.reviewing = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # -- outcomes -------------------------------------------------------------
    async def evaluate_outcomes(self) -> int:
        now = now_utc()
        window = dict(older_than=(now - timedelta(hours=16)).isoformat(timespec="seconds"),
                      newer_than=(now - timedelta(days=45)).isoformat(timespec="seconds"))
        todo = self.db.proposals_needing_outcomes(**window)
        sig_todo = self.db.signals_needing_outcomes(**window)
        if not todo and not sig_todo:
            return 0
        bars: dict[str, list[dict]] = {}
        for sym in {p["symbol"] for p in todo + sig_todo} | {BENCHMARK}:
            try:
                bars[sym] = parse_bars(await self.broker.call(
                    "get_stock_bars", symbols=sym, timespan="D", count=80))
            except BrokerError as e:
                log.warning("bars for %s failed: %s", sym, e)
                bars[sym] = []

        # Reference (documented): both legs are close-to-close on completed DAILY bars. The
        # stock leg is entry price (ref_price/limit) -> later daily closes; SPY is the last daily
        # close on/before the entry day -> later daily closes. A bar dated today is ignored
        # until the session has closed (it is a partial, intraday value).
        _today = now_et().date()
        _close = session_close(_today)
        _done = _close is not None and now_et().time() >= _close
        today_cut = _today + timedelta(days=1) if (_done or _close is None) else _today

        def outcome(row: dict, entry: float | None, sign: int) -> dict:
            day = _entry_day(row["created_at"])
            after = [b for b in bars.get(row["symbol"], []) if day < bar_day(b["time"]) < today_cut]
            if not after or not entry:
                return {}
            fields: dict = {}
            if row["ret_1d"] is None:
                fields["ret_1d"] = round(sign * (after[0]["close"] / entry - 1) * 100, 3)
            if len(after) >= 5:
                fields["ret_5d"] = round(sign * (after[4]["close"] / entry - 1) * 100, 3)
                spy = bars.get(BENCHMARK, [])
                base = [b for b in spy if bar_day(b["time"]) <= day]
                fwd = [b for b in spy if day < bar_day(b["time"]) < today_cut]
                if base and len(fwd) >= 5:
                    fields["spy_5d"] = round((fwd[4]["close"] / base[-1]["close"] - 1) * 100, 3)
            if fields:
                fields["evaluated_at"] = now.isoformat(timespec="seconds")
            return fields

        updated = 0
        for p in todo:
            fields = outcome(p, p["ref_price"] or p["limit_price"], 1 if p["side"] == "BUY" else -1)
            if fields:
                self.db.update_proposal(p["id"], **fields)
                updated += 1
        n_sig = 0
        for sgl in sig_todo:   # signals are scored as if bought at the snapshot price
            fields = outcome(sgl, sgl["close"], 1)
            if fields:
                self.db.update_signal(sgl["id"], **fields)
                n_sig += 1
        if updated or n_sig:
            self.db.log("info", f"Learning: scored outcomes for {updated} proposal(s) and "
                                f"{n_sig} signal snapshot(s)")
        return updated + n_sig

    # -- scorecard --------------------------------------------------------------
    def stats(self) -> dict:
        rows = self.db.evaluated(500)

        def summary(items: list[dict]) -> dict:
            r = [x["ret_5d"] for x in items]
            return {
                "n": len(r),
                "hit_rate": round(sum(v > 0 for v in r) / len(r) * 100, 1) if r else None,
                "avg_5d": round(sum(r) / len(r), 3) if r else None,
            }

        groups: dict[str, list[dict]] = defaultdict(list)
        for x in rows:
            groups["side:" + x["side"]].append(x)
            groups["acted" if x["status"] == "placed" else "not_acted"].append(x)
            c = x["confidence"] or 0
            groups["conf:" + ("high" if c >= 0.7 else "mid" if c >= 0.5 else "low")].append(x)
        buys = [x for x in rows if x["side"] == "BUY" and x["spy_5d"] is not None]
        if buys:
            avg_buy_5d = round(sum(x["ret_5d"] for x in buys) / len(buys), 3)
            avg_spy_5d = round(sum(x["spy_5d"] for x in buys) / len(buys), 3)
            # Per-trade alpha: average of (trade_return - spy_return) for each trade
            alphas = [x["ret_5d"] - x["spy_5d"] for x in buys]
            avg_alpha = round(sum(alphas) / len(alphas), 3)
            # Also track win rate of alpha
            alpha_hit_rate = round(sum(a > 0 for a in alphas) / len(alphas) * 100, 1)
        else:
            avg_buy_5d = avg_spy_5d = avg_alpha = alpha_hit_rate = None

        return {
            "all": summary(rows),
            "groups": {k: summary(v) for k, v in sorted(groups.items())},
            "buys_vs_spy": {
                "n": len(buys),
                "avg_buy_5d": avg_buy_5d,
                "avg_spy_5d": avg_spy_5d,
                "avg_alpha_5d": avg_alpha,
                "alpha_hit_rate": alpha_hit_rate,
            },
        }

    def analytics(self) -> dict:
        """Calibration and signal attribution for the Learning dashboard."""
        props = self.db.evaluated(500)
        sigs = self.db.evaluated_signals(2000)
        spy = [x["spy_5d"] for x in sigs if x.get("spy_5d") is not None]
        return {
            "confidence_bins": _bins(props, "confidence", [0, 0.5, 0.6, 0.7, 0.8, 1.01]),
            "ml_bins": _bins(sigs, "ml_p", [0, 0.45, 0.5, 0.55, 0.6, 1.01]),
            "ta_bins": _bins(sigs, "ta_score", [-1.01, -0.5, -0.15, 0.15, 0.5, 1.01]),
            "sentiment_bins": _bins(sigs, "sent_score", [-1.01, -0.25, 0.0, 0.25, 1.01]),
            "consensus": {
                "long": _summary([x for x in sigs if x["consensus"] == "long"]),
                "blocked": _summary([x for x in sigs if x["consensus"] == "blocked"]),
                "spy_avg_5d": round(sum(spy) / len(spy), 3) if spy else None,
            },
            "by_trend": {t: _summary([x for x in sigs if x["trend"] == t])
                         for t in ("up", "range", "down")},
            "signals_scored": len(sigs),
        }

    # -- review (Groq) -------------------------------------------------------------
    def _review_prompt(self, stats: dict) -> str:
        rows = self.db.evaluated(40)
        lines = []
        for x in rows:
            feats = json.loads(x["features"]) if x["features"] else {}
            keep = ("rsi14", "pct_vs_sma20", "pct_vs_sma50", "pct_from_high10", "atr_pct",
                    "chg_5d_pct", "vol_vs_20d_avg")
            f = ", ".join(f"{k}={feats[k]}" for k in keep if feats.get(k) is not None)
            gate = ""
            if x.get("consensus"):
                try:
                    fails = [c["name"] for c in json.loads(x["consensus"]) if not c["ok"]]
                    gate = f" gate={'pass' if not fails else 'fail:' + '/'.join(fails)}"
                except (ValueError, TypeError, KeyError):
                    pass
            ml = f" ml_p={x['ml_p']}" if x.get("ml_p") is not None else ""
            lines.append(
                f"- #{x['id']} {x['created_at'][:10]} {x['side']} {x['symbol']} "
                f"conf={x['confidence']} status={x['status']}{gate}{ml} | {f or 'no indicators'} | "
                f"1d {_fmt(x['ret_1d'])}% 5d {_fmt(x['ret_5d'])}% SPY5d {_fmt(x['spy_5d'])}% | "
                f"why: {sanitize_headline(x['rationale'], 240)}")
        lessons = LESSONS_FILE.read_text(encoding="utf-8").strip() if LESSONS_FILE.exists() else ""
        strategy = STRATEGY_FILE.read_text(encoding="utf-8").strip() if STRATEGY_FILE.exists() else ""
        return (f"## Strategy\n{strategy or '(none)'}\n\n"
                f"## Current lessons\n{lessons or '(none yet)'}\n\n"
                f"## Statistics (5-day outcomes)\n{json.dumps(stats, indent=1)}\n\n"
                f"## Recent proposals with outcomes (newest first)\n" + "\n".join(lines) +
                "\n\nWrite the updated lessons.")

    async def run_review(self, trigger: str) -> dict:
        if self._lock.locked():
            raise ValueError("A review is already running")
        async with self._lock:
            self.reviewing = True
            try:
                return await self._review(trigger)
            finally:
                self.reviewing = False

    async def _review(self, trigger: str) -> dict:
        stats = self.stats()
        n = stats["all"]["n"]
        if n < self.s.min_outcomes_for_review:
            raise ValueError(f"Need at least {self.s.min_outcomes_for_review} scored proposals "
                             f"for a review (have {n}). Outcomes are scored 1-5 trading days "
                             "after each proposal.")
        try:
            response = await chat_completion(
                self.s,
                messages=[{"role": "system", "content": REVIEW_SYSTEM},
                          {"role": "user", "content": self._review_prompt(stats)}],
                json_schema=("outcome_review", REVIEW_SCHEMA), max_tokens=2048)
            total_in, output_tokens, cache_read = usage_counts(response)
            model = provider_model(response, self.s.llm_model)
            cost = estimate_cost(model, total_in, output_tokens, cache_read)
            self.db.add_usage("review", model, total_in, output_tokens,
                              cache_read, 0, cost)
            out = parse_json_response(response)
        except Exception as e:  # noqa: BLE001 - record and surface in the UI
            err = f"{type(e).__name__}: {e}"
            self.db.add_review(trigger, "error", n_outcomes=n, stats=json.dumps(stats), error=err)
            self.db.log("error", f"Learning review failed: {err}")
            raise RuntimeError(err) from e

        out, problems = validate_review(out)
        if out is None:
            err = "invalid review output: " + "; ".join(problems)
            self.db.add_review(trigger, "error", n_outcomes=n, stats=json.dumps(stats), error=err)
            self.db.log("error", f"Learning review rejected: {err}")
            raise RuntimeError(err)
        for why in problems:
            self.db.log("warn", f"Learning review item skipped/trimmed: {why}")

        # LLM text is a PROPOSAL: a draft file for the owner, never lessons.md.
        draft = ("<!-- PROPOSED DRAFT written by the LLM learning reviewer on "
                 f"{now_utc().isoformat(timespec='seconds')}. Nothing reads this file. "
                 "Copy what you agree with into lessons.md. -->\n")
        try:
            proposed_lessons_path().write_text(draft + out["lessons"].strip() + "\n",
                                               encoding="utf-8")
        except OSError as e:
            self.db.log("warn", f"Could not write lessons draft: {e}")
        self.db.add_review(trigger, "ok", n_outcomes=n, stats=json.dumps(stats),
                           lessons=out["lessons"], suggestions=out["strategy_suggestions"],
                           evidence=out["evidence_strength"],
                           input_tokens=total_in, output_tokens=output_tokens)
        n_params = 0
        for sug in out["parameter_suggestions"]:
            self.db.add_lesson(param_name=sug["param_name"], param_value=sug["value"],
                               param_scope="global", sample_size=0,
                               evidence_summary="LLM review suggestion (unverified): "
                                                + sug["rationale"],
                               status="proposed", created_by="review")
            n_params += 1
        self.db.log("info", f"Learning review done ({n} outcomes, evidence "
                            f"{out['evidence_strength']}). Draft saved to "
                            f"{proposed_lessons_path().name}; {n_params} parameter suggestion(s) "
                            "stored as 'proposed' (human approval required).")
        try:
            from .lessons import propose_lessons_from_autopsies
            n_new = propose_lessons_from_autopsies(self.db, self.s)
            if n_new:
                self.db.log("info", f"{n_new} parameter lesson(s) proposed; approve them in the dashboard")
        except Exception as e:  # noqa: BLE001 - never break the review
            self.db.log("warn", f"Parameter lesson proposal failed: {e}")
        return out

    # -- nightly job (Phase 7) ----------------------------------------------------
    async def run_nightly_job(self, engine: Engine | None = None) -> dict:
        """Run the complete nightly learning cycle after market close.

        This is the Phase 7 "close learning loop" job that:
        1. Evaluates outcomes for pending proposals/signals
        2. Runs LLM review if enough outcomes accumulated
        3. Applies active lessons to engine settings (guardrailed)
        4. Retrains ML model if due
        5. Proposes parameter lessons from trade autopsies
        6. Returns summary of all actions taken

        Args:
            engine: Optional Engine instance to apply lessons to immediately

        Returns:
            Dict with results of each step
        """
        results = {
            "timestamp": now_utc().isoformat(timespec="seconds"),
            "outcomes_evaluated": 0,
            "review": None,
            "lessons_applied": 0,
            "ml_retrained": False,
            "parameter_proposals": 0,
            "errors": [],
        }

        # 1. Evaluate outcomes (free, no LLM)
        try:
            if self.broker.connected:
                results["outcomes_evaluated"] = await self.evaluate_outcomes()
                self.db.log("info", f"Nightly job: evaluated {results['outcomes_evaluated']} outcome(s)")
        except Exception as e:
            err = f"Outcome evaluation failed: {type(e).__name__}: {e}"
            results["errors"].append(err)
            self.db.log("error", f"Nightly job: {err}")

        # 2. Run LLM review if due
        try:
            if self._review_due():
                self.db.log("info", "Nightly job: running scheduled review")
                review_result = await self.run_review("nightly")
                results["review"] = {
                    "evidence_strength": review_result.get("evidence_strength"),
                    "lessons": review_result.get("lessons")[:200] + "..." if len(review_result.get("lessons", "")) > 200 else review_result.get("lessons"),
                    "strategy_suggestions": review_result.get("strategy_suggestions"),
                }
            else:
                stats = self.stats()
                self.db.log("info", f"Nightly job: review not due (need {self.s.min_outcomes_for_review} outcomes, have {stats['all']['n']}; review every {self.s.review_every_days} days)")
        except Exception as e:
            err = f"Review failed: {type(e).__name__}: {e}"
            results["errors"].append(err)
            self.db.log("error", f"Nightly job: {err}")

        # 3. Apply active lessons to engine settings (guardrailed)
        try:
            if engine is not None:
                results["lessons_applied"] = engine.refresh_lessons()
                if results["lessons_applied"] > 0:
                    self.db.log("info", f"Nightly job: applied {results['lessons_applied']} lesson(s) to engine settings")
        except Exception as e:
            err = f"Lesson application failed: {type(e).__name__}: {e}"
            results["errors"].append(err)
            self.db.log("error", f"Nightly job: {err}")

        # 4. Retrain ML model if due
        try:
            if self._retrain_due():
                watchlist = self.research.effective_watchlist() if self.research else self.s.watchlist
                train_result = await self.ml.train(self.broker, watchlist)
                results["ml_retrained"] = True
                results["ml_result"] = {
                    "id": train_result.get("id"),
                    "status": train_result.get("status"),
                    "mean_auc": train_result.get("metrics", {}).get("mean_auc"),
                }
                self.db.log("info", f"Nightly job: ML model retrained (status: {train_result.get('status')})")
        except Exception as e:
            err = f"ML retrain failed: {type(e).__name__}: {e}"
            results["errors"].append(err)
            self.db.log("error", f"Nightly job: {err}")

        # 5. Propose parameter lessons from autopsies
        try:
            n_proposals = propose_lessons_from_autopsies(self.db, self.s)
            results["parameter_proposals"] = n_proposals
            if n_proposals > 0:
                self.db.log("info", f"Nightly job: {n_proposals} parameter lesson(s) proposed from autopsies")
        except Exception as e:
            err = f"Parameter lesson proposal failed: {type(e).__name__}: {e}"
            results["errors"].append(err)
            self.db.log("error", f"Nightly job: {err}")

        # Log nightly job completion
        self.db.log("info", f"Nightly job complete: outcomes={results['outcomes_evaluated']}, "
                         f"review={'yes' if results['review'] else 'no'}, "
                         f"lessons_applied={results['lessons_applied']}, "
                         f"ml_retrained={results['ml_retrained']}, "
                         f"param_proposals={results['parameter_proposals']}, "
                         f"errors={len(results['errors'])}")
        return results

    # -- background loop ---------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="learning")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                if self.broker.connected:
                    await self.evaluate_outcomes()
                if self._review_due():
                    await self.run_review("auto")
                if self._retrain_due():
                    watchlist = self.research.effective_watchlist() if self.research else self.s.watchlist
                    await self.ml.train(self.broker, watchlist)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("learning loop")
            await asyncio.sleep(3600)

    def _retrain_due(self) -> bool:
        if (self.ml is None or not self.ml.available or self.ml.training
                or self.s.ml_retrain_days <= 0 or market_is_open() or not self.broker.connected):
            return False
        models = self.db.models(1)
        if not models:
            return True
        age = now_utc() - datetime.fromisoformat(models[0]["created_at"])
        return age >= timedelta(days=self.s.ml_retrain_days)

    def _review_due(self) -> bool:
        if self.s.review_every_days <= 0 or market_is_open():
            return False
        if self.stats()["all"]["n"] < self.s.min_outcomes_for_review:
            return False
        last = self.db.last_ok_review()
        if last is None:
            return True
        age = now_utc() - datetime.fromisoformat(last["created_at"])
        return age >= timedelta(days=self.s.review_every_days)
