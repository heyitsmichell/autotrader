"""Shadow-to-live promotion check (never automatic, never places orders).

Compares a shadow's FORWARD equity history (shadow_equity) with a live sleeve's (sleeve_equity) over the
days both have data. Pass requires ALL of: enough history, better annualised Sharpe, drawdown no worse.
"""
from __future__ import annotations

import math

MIN_DAYS = 60
PERIODS = 252


def _stats(eq: list[float]) -> dict:
    rets = [b / a - 1 for a, b in zip(eq, eq[1:]) if a]
    sharpe = None
    if len(rets) >= 2:
        m = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
        sharpe = m / sd * math.sqrt(PERIODS) if sd > 0 else None
    peak, mdd = -math.inf, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1) if peak > 0 else mdd
    return {"rets": rets, "sharpe": sharpe, "max_drawdown": mdd}


def _corr(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def promotion_check(shadow_id: str, target: str, shadow_series: list[dict], sleeve_series: list[dict],
                    min_days: int = MIN_DAYS) -> dict:
    """Pure core: series are rows {"day", <id>: equity} as returned by the DB. Use ``check_from_db``."""
    sh = {r["day"]: r[shadow_id] for r in shadow_series if r.get(shadow_id)}
    tg = {r["day"]: r[target] for r in sleeve_series if r.get(target)}
    days = sorted(set(sh) & set(tg))
    s_eq, t_eq = [sh[d] for d in days], [tg[d] for d in days]
    s, t = _stats(s_eq), _stats(t_eq)
    n_days = len(days)                       # trading days with both histories (returns = n_days - 1)
    corr = _corr(s["rets"], t["rets"])

    def crit(name, label, value, tv, ok, gate=True):
        return {"id": name, "label": label, "value": value, "target_value": tv, "pass": ok, "gate": gate}

    c_days = crit("days", f"At least {min_days} trading days of history", n_days, min_days, n_days >= min_days)
    ok_sh = s["sharpe"] is not None and (t["sharpe"] is None or s["sharpe"] > t["sharpe"])
    c_sh = crit("sharpe", "Sharpe higher than the live sleeve",
                None if s["sharpe"] is None else round(s["sharpe"], 3),
                None if t["sharpe"] is None else round(t["sharpe"], 3), ok_sh)
    ok_dd = n_days >= 2 and s["max_drawdown"] >= t["max_drawdown"]
    c_dd = crit("drawdown", "Max drawdown no worse than the live sleeve",
                round(s["max_drawdown"], 4), round(t["max_drawdown"], 4), ok_dd)
    c_corr = crit("correlation", "Correlation with the live sleeve (info only)",
                  None if corr is None else round(corr, 3), None, True, gate=False)
    criteria = [c_days, c_sh, c_dd, c_corr]
    passed = all(c["pass"] for c in criteria if c["gate"])
    if not c_days["pass"]:
        verdict = (f"Not ready: {n_days} of {min_days} trading days of history so far; "
                   f"{max(min_days - n_days, 0)} more needed before this can be judged.")
    elif passed:
        verdict = (f"Passes: over {n_days} trading days it beat {target} on Sharpe and drawdown. "
                   "Promotion still needs your explicit confirmation and a code change.")
    else:
        failed = [c["label"] for c in criteria if c["gate"] and not c["pass"]]
        verdict = "Does not pass: " + "; ".join(failed).lower() + "."
    return {"shadow": shadow_id, "target": target, "days": n_days, "min_days": min_days,
            "criteria": criteria, "passed": passed, "verdict": verdict,
            "first_day": days[0] if days else None, "last_day": days[-1] if days else None}


def check_from_db(db, shadow_id: str, target: str) -> dict:
    return promotion_check(shadow_id, target, db.shadow_equity_series(3650), db.sleeve_equity_series(3650))
