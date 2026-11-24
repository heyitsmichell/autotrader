"use client";

/** Last-resort error UI (QA H8): replaces the root layout, so it carries its own html/body and styles. */
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <html lang="en">
    <body style={{ margin: 0, background: "#0b1014", color: "#e7edf0", font: "14px/1.5 system-ui, sans-serif" }}>
      <main role="alert" style={{ maxWidth: 640, margin: "64px auto", padding: "0 16px" }}>
        <h1 style={{ fontSize: 20 }}>Trader dashboard failed to load</h1>
        <p style={{ color: "#a3b1bb" }}>{error.message || "Unexpected error"}</p>
        <p>
          The trading engine runs in the backend and is not affected. To stop trading without the dashboard,
          run <code>python scripts/trader.py stop</code> (or <code>Stop_Trader.bat</code>).
        </p>
        <button onClick={reset} style={{ padding: "8px 14px", font: "inherit", cursor: "pointer" }}>Try again</button>
      </main>
    </body>
  </html>;
}
