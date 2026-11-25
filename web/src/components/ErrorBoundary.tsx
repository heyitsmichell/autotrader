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
    return <section className={css["panel-error"]} role="alert">
      <p>{this.props.name} could not be shown ({this.state.error.message || "unexpected error"}). The kill switch still works.</p>
      <button className="button" onClick={() => this.setState({ error: null })}>Retry</button>
    </section>;
  }
}
