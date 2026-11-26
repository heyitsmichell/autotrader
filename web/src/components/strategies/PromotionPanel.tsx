"use client";

import { useState } from "react";
import { usePoll } from "@/lib/usePoll";
import type { Api } from "@/lib/api";
import type { ConfirmRequest } from "@/components/ConfirmModal";
import type { PromotionCheck, PromotionCriterion, Shadow } from "@/lib/types";
import css from "./Strategies.module.css";

const TARGETS = ["swing", "momentum", "putwrite", "vaa"];

const show = (c: PromotionCriterion, v: number | null) => {
  if (v == null) return "—";
  if (c.id === "drawdown") return `${(v * 100).toFixed(1)}%`;
  if (c.id === "days") return String(v);
  return v.toFixed(2);
};

export default function PromotionPanel({ api, shadow, onConfirm, onAction }: {
  api: Api; shadow: Shadow;
  onConfirm?: (r: ConfirmRequest | null) => void;
  onAction?: (label: string, fn: () => Promise<unknown>) => void | Promise<void>;
}) {
  const [target, setTarget] = useState("swing");
  const [msg, setMsg] = useState("");
  const poll = usePoll<PromotionCheck>(api, `/api/promotion/${encodeURIComponent(shadow.id)}?target=${target}`, 3_600_000);
  const d = poll.data && poll.data.shadow === shadow.id && poll.data.target === target ? poll.data : null;

  const send = async (capital: number) => {
    const fn = async () => {
      await api(`/api/promotion/${encodeURIComponent(shadow.id)}`, "POST",
        { target, capital_usd: capital, confirm: "PROMOTE" });
      setMsg("Promotion request recorded. No orders were placed: a code change and deploy is still needed.");
    };
    if (onAction) await onAction(`${shadow.name} promotion request`, fn); else await fn();
  };
  const promote = () => {
    const r: ConfirmRequest = {
      title: `Promote ${shadow.name}?`, confirmLabel: "Promote", danger: true, typeToConfirm: "PROMOTE",
      message: <p>This records a request to turn {shadow.name} into a live sleeve compared against {target}. It places
        no orders; a code change and deploy is still required. Enter the capital in USD to allocate.</p>,
      input: { label: "Capital (USD)", initial: "10000", required: true },
      onConfirm: async (v?: string) => {
        onConfirm?.(null);
        const cap = Number(String(v ?? "").replace(/[,$\s]/g, ""));
        if (!(cap > 0)) { setMsg("Enter a positive capital amount."); return; }
        await send(cap);
      },
    };
    if (onConfirm) onConfirm(r);
  };

  return <div>
    <h3>Promotion check</h3>
    <p className={css["muted"]}>Compared with live sleeve{" "}
      <select value={target} onChange={e => { setTarget(e.target.value); setMsg(""); }} aria-label="Target live sleeve">
        {TARGETS.map(t => <option key={t} value={t}>{t}</option>)}
      </select></p>
    {poll.error && !d && <p className={css["muted"]}>Could not run the check for this target.</p>}
    {!d && !poll.error && <p className={css["muted"]}>Loading…</p>}
    {d && <>
      <p>{d.days} / {d.min_days} trading days</p>
      <div className={css["scroll"]}>
        <table className={css["dtbl"]}>
          <thead><tr><th>Criterion</th><th>Shadow</th><th>Live</th><th>Result</th></tr></thead>
          <tbody>{d.criteria.map(c => <tr key={c.id}><td>{c.label}</td><td>{show(c, c.value)}</td>
            <td>{c.id === "correlation" ? "—" : show(c, c.target_value)}</td>
            <td>{c.gate ? (c.pass ? "Pass" : "Fail") : "Info"}</td></tr>)}</tbody>
        </table></div>
      <p className={css["muted"]}>{d.verdict}</p>
      <p><button type="button" className={css["linkBtn"]} disabled={!d.passed} onClick={promote}
        title={d.passed ? "Request promotion" : "Enabled only when every criterion passes"}>Promote</button></p>
    </>}
    {msg && <p role="status">{msg}</p>}
  </div>;
}
