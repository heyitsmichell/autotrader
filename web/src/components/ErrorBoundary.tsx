"use client";

/**
 * Per-panel error boundary (QA H8). A crash in one panel shows a small card with a retry
 * button instead of blanking the whole dashboard; the top bar (kill switch, flatten) lives
 * outside every boundary so it stays usable.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";
import css from "./ErrorBoundary.module.css";

interface Props {
  name: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[${this.props.name}] panel crashed`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return <section className={`panel ${css["panel-error"]}`} role="alert">
      <div className="eyebrow">PANEL ERROR</div>
      <h2>{this.props.name} could not be shown</h2>
      <p className="muted">{this.state.error.message || "Unexpected error"}. The rest of the dashboard,
        including the kill switch, still works.</p>
      <button className="button button-quiet" onClick={() => this.setState({ error: null })}>Try again</button>
    </section>;
  }
}
