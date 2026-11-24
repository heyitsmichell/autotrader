"""AI runs"""

from __future__ import annotations

import json

from .schema import _now
from .base import BaseRepo


class RunsRepo(BaseRepo):
    """Mixin for the DB facade: ai runs."""

    # -- runs --------------------------------------------------------------
    def create_run(self, trigger: str, model: str | None = None) -> int:
        return self._insert("runs", {
            "trigger": trigger, "started_at": _now(), "model": model,
            "broker_mode": self.broker_mode, "webull_environment": self.webull_environment, "account_id": self.account_id
        })

    def close_orphan_runs(self) -> int:
        """Runs still marked 'running' when the app starts were cut off by a crash/restart."""
        ns_where, ns_args = self._ns_where()
        cur = self.conn.execute(
            f"UPDATE runs SET status='error', finished_at=?, error='interrupted (app restarted)' "
            f"WHERE status='running' AND {ns_where}", (_now(), *ns_args))
        return cur.rowcount or 0

    def finish_run(self, run_id: int, status: str, *, summary: str | None = None,
                   error: str | None = None, tool_calls: list[str] | None = None,
                   input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0,
                   cache_write: int = 0, cost_usd: float | None = None) -> None:
        self._update("runs", run_id, {
            "finished_at": _now(), "status": status, "summary": summary, "error": error,
            "tool_calls": ", ".join(tool_calls or []), "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write, "cost_usd": cost_usd,
        })

    def set_run_signals(self, run_id: int, n: int) -> None:
        self._update("runs", run_id, {"n_signals": n})

    def runs(self, limit: int = 10) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT r.*, (SELECT COUNT(*) FROM proposals p WHERE p.run_id=r.id AND {ns_where}) AS n_proposals, "
            f"(SELECT COUNT(*) FROM proposals p WHERE p.run_id=r.id AND p.status NOT IN "
            f"('rejected') AND {ns_where}) AS n_accepted FROM runs r WHERE {ns_where} ORDER BY r.id DESC LIMIT ?", *ns_args, *ns_args, *ns_args, limit)

    # -- effective parameters per run (M9 provenance) -------------------------------------
    def save_run_params(self, run_id: int, params: dict) -> None:
        self.conn.execute(
            "INSERT INTO run_params (run_id, params, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET params=excluded.params",
            (run_id, json.dumps(params, default=str, sort_keys=True), _now()))

    def get_run_params(self, run_id: int) -> dict | None:
        row = self._one("SELECT params FROM run_params WHERE run_id=?", run_id)
        if row is None:
            return None
        try:
            return json.loads(row["params"])
        except ValueError:
            return None
