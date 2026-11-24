"""Lessons (versioned parameter proposals)"""

from __future__ import annotations

from typing import Any

from .schema import _now, decode
from .base import BaseRepo


class LessonsRepo(BaseRepo):
    """Mixin for the DB facade: lessons (versioned parameter proposals)."""

    def add_lesson(self, **fields: Any) -> int:
        fields.update({
            "created_at": _now(),
            "broker_mode": self.broker_mode,
            "webull_environment": self.webull_environment,
            "account_id": self.account_id,
        })
        return self._insert("lessons", fields)

    def get_lesson(self, lesson_id: int) -> dict | None:
        ns_where, ns_args = self._ns_where()
        row = self._one(f"SELECT * FROM lessons WHERE id=? AND {ns_where}", lesson_id, *ns_args)
        return decode(row)

    def lessons(self, status: str | None = None, param_name: str | None = None,
                limit: int = 50) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        where = [ns_where]
        args = list(ns_args)
        if status:
            where.append("status=?")
            args.append(status)
        if param_name:
            where.append("param_name=?")
            args.append(param_name)
        sql = f"SELECT * FROM lessons WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
        rows = self._rows(sql, *args, limit)
        return [decode(r) for r in rows]

    def active_lessons(self) -> list[dict]:
        """Get currently active lessons (for applying to engine)."""
        ns_where, ns_args = self._ns_where()
        rows = self._rows(f"SELECT * FROM lessons WHERE status='active' AND {ns_where} ORDER BY id", *ns_args)
        return [decode(r) for r in rows]

    def set_lesson_status(self, lesson_id: int, status: str,
                          approved_by: str | None = None, retired_reason: str | None = None) -> None:
        ns_where, ns_args = self._ns_where()
        fields = {"status": status, "updated_at": _now()}
        if status == "approved":
            fields["approved_by"] = approved_by
            fields["approved_at"] = _now()
        elif status == "active":
            fields["activated_at"] = _now()
        elif status == "retired":
            fields["retired_at"] = _now()
            fields["retired_reason"] = retired_reason
            fields["retired_by"] = approved_by
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE lessons SET {cols} WHERE id=? AND {ns_where}", (*fields.values(), lesson_id, *ns_args))

    def create_lesson_version(self, old_lesson_id: int, new_value: float,
                              created_by: str = "human") -> int:
        """Create a new version of an existing lesson (for rollback capability)."""
        old = self.get_lesson(old_lesson_id)
        if not old:
            raise ValueError(f"Lesson {old_lesson_id} not found")
        # Retire the old version
        self.set_lesson_status(old_lesson_id, "retired", retired_by=created_by, retired_reason=f"Superseded by new version")
        # Create new version
        fields = {
            "version": old["version"] + 1,
            "param_name": old["param_name"],
            "param_value": new_value,
            "param_scope": old["param_scope"],
            "sample_size": old["sample_size"],
            "min_sample_size": old["min_sample_size"],
            "train_pct": old["train_pct"],
            "val_pct": old["val_pct"],
            "test_pct": old["test_pct"],
            "baseline_metric": old["baseline_metric"],
            "proposed_metric": old["proposed_metric"],
            "metric_name": old["metric_name"],
            "p_value": old["p_value"],
            "supporting_autopsies": old["supporting_autopsies"],
            "evidence_summary": old["evidence_summary"],
            "status": "proposed",
            "created_by": created_by,
            "previous_version_id": old_lesson_id,
        }
        return self.add_lesson(**fields)

    def rollback_lesson(self, lesson_id: int, rolled_back_by: str) -> int | None:
        """Rollback to previous version."""
        lesson = self.get_lesson(lesson_id)
        if not lesson or not lesson.get("previous_version_id"):
            return None
        # Retire current
        self.set_lesson_status(lesson_id, "retired", retired_by=rolled_back_by, retired_reason="Rolled back")
        # Reactivate previous
        prev = self.get_lesson(lesson["previous_version_id"])
        if prev:
            self.set_lesson_status(prev["id"], "active")
            return prev["id"]
        return None

    # -- apply tracking (M9): every lesson ends up applied or skipped, with the reason ------
    def record_lesson_skip(self, lesson_id: int, reason: str) -> None:
        """The auto-apply step looked at this lesson and did not apply it. Say why."""
        ns_where, ns_args = self._ns_where()
        self.conn.execute(
            f"UPDATE lessons SET apply_state='skipped', apply_reason=?, updated_at=? "
            f"WHERE id=? AND {ns_where}", (reason, _now(), lesson_id, *ns_args))

    def record_lesson_applied(self, lesson_id: int, reason: str = "") -> None:
        """The lesson's value was applied to the live settings."""
        ns_where, ns_args = self._ns_where()
        self.conn.execute(
            f"UPDATE lessons SET apply_state='applied', apply_reason=?, updated_at=? "
            f"WHERE id=? AND {ns_where}", (reason or None, _now(), lesson_id, *ns_args))
