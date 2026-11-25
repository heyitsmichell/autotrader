"use client";

import { useState } from "react";
import { money, pct } from "@/lib/format";
import type { Sleeve } from "@/lib/types";
import { sleeveColor } from "./sleeveColors";
import css from "./Strategies.module.css";

export default function AllocationBar({ sleeves }: { sleeves: Sleeve[] }) {
  const [hover, setHover] = useState<string | null>(null);
  const total = sleeves.reduce((a, s) => a + (s.equity || 0), 0);
  if (!sleeves.length) return null;
  const share = (s: Sleeve) => (total > 0 ? (s.equity || 0) / total * 100 : 100 / sleeves.length);
  const h = sleeves.find(s => s.id === hover);
  const hi = h ? sleeves.indexOf(h) : -1;
  return <div>
    <div className={css["bar"]} role="img"
      aria-label={`Allocation: ${sleeves.map(s => `${s.name} ${pct(share(s), { sign: false, digits: 1 })}`).join(", ")}`}>
      {sleeves.map(s => <div key={s.id} className={css["seg"]}
        style={{ flex: `${Math.max(share(s), 0.5)} 1 0`, background: sleeveColor(s.id, s.color_slot) }}
        onMouseEnter={() => setHover(s.id)} onMouseLeave={() => setHover(null)}
        title={`${s.name}: ${money(s.equity)} (${pct(share(s), { sign: false, digits: 1 })})`}/>)}
      {sleeves.slice(1).map((s, i) => <span key={s.id} className={css["tick"]}
        style={{ left: `${(i + 1) * 100 / sleeves.length}%` }} aria-hidden="true"/>)}
      {h && <div className={css["tip"]} style={{ left: `${Math.min(80, hi * 100 / sleeves.length)}%` }}>
        {h.name}: {money(h.equity)} · {pct(share(h), { sign: false, digits: 1 })} of sleeves
      </div>}
    </div>
    <div className={css["labels"]}>
      {sleeves.map(s => <div key={s.id} className={css["lab"]}>
        <b><i className={css["chip"]} style={{ background: sleeveColor(s.id, s.color_slot) }}/>{s.name}</b>
        {money(s.equity)} · {pct(share(s), { sign: false, digits: 1 })}
      </div>)}
    </div>
  </div>;
}
