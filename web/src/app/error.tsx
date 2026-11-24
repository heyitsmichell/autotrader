"use client";

import { useEffect } from "react";

/** Route-level error UI (QA H8): shown when the dashboard itself fails to render. */
export default function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => { console.error("dashboard crashed", error); }, [error]);
  return <main className="app-shell fatal-error" role="alert">
    <section className="panel">
      <div className="eyebrow">DASHBOARD ERROR</div>
      <h2>The dashboard hit an error</h2>
      <p className="muted">{error.message || "Unexpected error"}</p>
      <p>
        The trading engine runs in the backend and is not affected. To stop trading without the dashboard,
        run <code>python scripts/trader.py stop</code> (or <code>Stop_Trader.bat</code>).
      </p>
      <div className="inline-actions">
        <button className="button button-primary" onClick={reset}>Reload dashboard</button>
        <button className="button button-quiet" onClick={() => window.location.reload()}>Full page reload</button>
      </div>
    </section>
  </main>;
}
