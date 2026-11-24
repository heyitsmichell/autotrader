"use client";

import { useCallback, useMemo, useState, type ReactNode } from "react";
import ApproveDrawer from "./ApproveDrawer";
import AutopsyView from "./AutopsyView";
import BannerRail from "./BannerRail";
import CommandBar from "./CommandBar";
import Today from "./Today";
import ConfirmModal, { ConfirmProvider, type ConfirmRequest } from "./ConfirmModal";
import DecisionModal from "./DecisionModal";
import ErrorBoundary from "./ErrorBoundary";
import HistoryTable from "./HistoryTable";
import LearningView from "./LearningView";
import MarketChart from "./MarketChart";
import MLScorecard from "./MLScorecard";
import OptionChain from "./OptionChain";
import OptionsStatus from "./OptionsStatus";
import ProposalQueue, { type ApproveRequest } from "./ProposalCard";
import ResearchPanel from "./ResearchPanel";
import SettingsPanel from "./SettingsPanel";
import { AccountPanel, HealthPanel, OrdersPanel } from "./SidePanels";
import SignalsTable from "./SignalsTable";
import TopBar, { type Tab } from "./TopBar";
import TradeLedgerView from "./TradeLedgerView";
import YahooFinancePanel from "./YahooFinancePanel";
import { useDashboardData } from "@/lib/useDashboardData";
import type { Proposal, ProposalDetail } from "@/lib/types";
import css from "./Dashboard.module.css";

const Panel = ({ name, children }: { name: string; children: ReactNode }) =>
  <ErrorBoundary name={name}>{children}</ErrorBoundary>;

export default function Dashboard() {
  const {
    mounted, status, portfolio, signals, proposals, runs, events, research, ml, learning, performance,
    strategy, setStrategy, lessons, setLessons, refresh, refreshMl, api, error, 
    duplicateInstance, streamConnected, strategyLoaded, lessonsLoaded,
  } = useDashboardData();

  const [selected, setSelected] = useState<Proposal | null>(null);
  const [detail, setDetail] = useState<ProposalDetail | null>(null);
  const [approving, setApproving] = useState<ApproveRequest | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>("today");

  const pending = useMemo(() => proposals.filter(p => p.status === "pending"), [proposals]);
  const positions = portfolio?.positions || [];
  const openOrders = portfolio?.open_orders || [];
  const live = !!status?.is_live;

  const perform = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setNotice("");
    try {
      await fn();
      setNotice(`${label} complete`);
      await refresh();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy("");
      window.setTimeout(() => setNotice(""), 6000);
    }
  }, [refresh]);

  const openProposal = useCallback(async (proposal: Proposal) => {
    setSelected(proposal);
    setDetail(null);
    try {
      setDetail(await api<ProposalDetail>(`/api/proposals/${proposal.id}`));
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "Unable to load proposal details");
    }
  }, [api]);

  const startFlatten = () => setConfirm({
    title: "Flatten everything?",
    confirmLabel: "Flatten all",
    danger: true,
    typeToConfirm: "FLATTEN",
    message: <>
      <p>This will immediately:</p>
      <ul>
        <li>turn on the kill switch,</li>
        <li>cancel <b>{openOrders.length}</b> open order(s), including stops and targets,</li>
        <li>sell <b>{positions.length}</b> open position(s) at about the market price.</li>
      </ul>
      <p className="muted">Broker: {status?.broker_label || "unknown"} · {live ? "LIVE account" : "paper"}.</p>
    </>,
    onConfirm: async () => {
      setConfirm(null);
      await perform("Flatten", () => api("/api/flatten", "POST", { confirm: "FLATTEN" }));
    },
  });

  const toggleKill = useCallback(() => {
    const turningOn = !status?.kill_switch;
    setConfirm(turningOn ? {
      title: "Turn on the kill switch?",
      confirmLabel: "Turn on kill switch",
      danger: true,
      message: <>
        <p>New orders are blocked and pending proposals are dismissed. The scheduler stops.</p>
        <p className="muted">Exits and protective stops still work. Orders already at the broker are not cancelled
          (use Flatten for that). {pending.length} pending proposal(s) are affected.</p>
      </>,
      onConfirm: async () => {
        setConfirm(null);
        await perform("Kill switch", () => api("/api/kill", "POST", { on: true, reason: "turned on from the dashboard" }));
      },
    } : {
      title: "Release the kill switch?",
      confirmLabel: "Release kill switch",
      danger: true,
      typeToConfirm: "RELEASE",
      message: <>
        <p>The kill switch was turned on because: <b>{status?.kill_reason || "no reason recorded"}</b>.</p>
        <p className="muted">New orders become possible again. The drawdown peak restarts from the current value.</p>
      </>,
      onConfirm: async () => {
        setConfirm(null);
        await perform("Kill switch released", () => api("/api/kill", "POST", { on: false, confirm: "RELEASE" }));
      },
    });
  }, [status?.kill_switch, status?.kill_reason, pending.length, perform, api]);

  const setMode = (mode: "approve" | "auto") => {
    if (status?.trading_mode === mode) return;
    if (mode === "approve") {
      void perform("Approval mode enabled", () => api("/api/mode", "POST", { mode }));
      return;
    }
    setConfirm({
      title: "Enable auto-trading?",
      confirmLabel: "Enable auto-trade",
      danger: true,
      typeToConfirm: live ? "AUTO" : undefined,
      message: <>
        <p>Qualifying proposals will be placed without approval.</p>
        <p className="muted">
          Broker: {status?.broker_label || "unknown"} · {live ? "LIVE account" : "paper"}.
          Order, position, daily-loss and drawdown limits still apply, and the kill switch stays available.
        </p>
      </>,
      onConfirm: async () => {
        setConfirm(null);
        await perform("Auto-trade enabled", () => api("/api/mode", "POST", { mode: "auto", confirm: "AUTO" }));
      },
    });
  };

  const setScheduler = (on: boolean) => {
    void perform(on ? "Engine resumed" : "Engine paused", () => api("/api/scheduler", "POST", { on }));
  };

  const approve = () => {
    if (!approving) return;
    const p = approving.proposal;
    setApproving(null);
    void perform(`Proposal #${p.id} submitted`, () => api(`/api/proposals/${p.id}/approve`, "POST"));
  };

  const lastRun = runs[0];
  const commandBar = <CommandBar
    status={status}
    lastRun={lastRun}
    busy={!!busy}
    onReconnect={() => void perform("Broker reconnected", () => api("/api/broker/reconnect", "POST"))}
    onRunNow={() => void perform("Analysis run started", () => api("/api/run", "POST"))}
    onTestAlert={() => void perform("Test alert sent", () => api("/api/alerts/test", "POST"))}
  />;
  const queue = <ProposalQueue
    kind="all"
    compact
    proposals={pending}
    status={status}
    api={api}
    busy={!!busy}
    onAction={perform}
    onOpen={p => void openProposal(p)}
    onApprove={setApproving}
  />;
  const chartSymbol = (symbol: string) => window.dispatchEvent(new CustomEvent("chart-symbol", { detail: symbol }));

  return <ConfirmProvider busy={!!busy}>
    <main className={`app-shell ${live ? `${css["live-frame"]}` : ""}`}>
      <TopBar
        status={status}
        busy={!!busy}
        tab={activeTab}
        onTab={setActiveTab}
        onMode={setMode}
        onFlatten={startFlatten}
        onKill={toggleKill}
        onScheduler={setScheduler}
      />
      <BannerRail status={status} error={error} streamConnected={streamConnected} pendingCount={pending.length}
        duplicateInstance={duplicateInstance}/>
      {notice && <div className={`${css["toast"]}`} role="status">{notice}</div>}

      <div className={`${css["main-grid"]}`}>
        <div className={`${css["primary-column"]}`}>
          {activeTab === "today" && <Panel name="Today">
            <Today
              status={status}
              portfolio={portfolio}
              performance={performance}
              events={events}
              pendingCount={pending.length}
              onSymbol={sym => { setActiveTab("research"); window.setTimeout(() => chartSymbol(sym), 50); }}
              needsYou={queue}
              controls={commandBar}
            />
          </Panel>}

          {activeTab === "research" && <>
            <Panel name="Chart"><MarketChart symbols={status?.watchlist || []} api={api} signals={signals}/></Panel>
            <Panel name="Signals">
              <SignalsTable signals={signals} status={status} ml={ml} onSelectSymbol={chartSymbol}/>
            </Panel>
            <Panel name="History">
              <HistoryTable proposals={proposals.slice(0, 10)} lastRun={lastRun} onOpen={p => void openProposal(p)}/>
            </Panel>
            <Panel name="Yahoo Finance">
              <YahooFinancePanel symbols={status?.watchlist || ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]} api={api}/>
            </Panel>
            <Panel name="Research"><ResearchPanel research={research}/></Panel>
          </>}

          {activeTab === "options" && status?.settings?.options_enabled && <>
            <Panel name="Option chain"><OptionChain status={status} api={api} onSymbolSelect={chartSymbol}/></Panel>
            <Panel name="Options status"><OptionsStatus status={status} api={api}/></Panel>
          </>}

          {activeTab === "learning" && <>
            <Panel name="Autopsies"><section className={`panel ${css["learning-panel"]}`}><AutopsyView/></section></Panel>
            <Panel name="Lessons"><section className={`panel ${css["learning-panel"]}`}><LearningView onAction={perform}/></section></Panel>
            <Panel name="ML scorecard">
              <section className={`panel ${css["learning-panel"]}`}>
                <MLScorecard ml={ml} onRefresh={refreshMl} onAction={perform} busy={!!busy}/>
              </section>
            </Panel>
            <Panel name="Trade ledger"><section className={`panel ${css["learning-panel"]}`}><TradeLedgerView/></section></Panel>
          </>}

          {activeTab === "settings" && <>
            <Panel name="Engine"><section className="panel"><h2>Engine</h2>{commandBar}</section></Panel>
            <Panel name="Account"><AccountPanel portfolio={portfolio} status={status} onRefresh={() => void refresh()}/></Panel>
            <Panel name="Orders"><OrdersPanel portfolio={portfolio}/></Panel>
            <Panel name="Health">
              <HealthPanel
                status={status}
                pendingCount={pending.length}
                busy={!!busy}
                onTestAlert={() => void perform("Test alert sent", () => api("/api/alerts/test", "POST"))}
              />
            </Panel>
            <Panel name="Reviewer notes">
              <SettingsPanel
                status={status}
                learning={learning}
                strategy={strategy}
                lessons={lessons}
                busy={!!busy}
                onStrategy={setStrategy}
                onLessons={setLessons}
                onAction={perform}
                api={api}
                strategyLoaded={strategyLoaded}
                lessonsLoaded={lessonsLoaded}
              />
            </Panel>
          </>}
        </div>
      </div>

      <footer className={`${css["app-footer"]}`}>
        <span>Times are in your local timezone. Shift+K opens the kill switch.</span>
        <span>{mounted ? status?.model || "AI analyst" : "AI analyst"}</span>
      </footer>

      {selected && <DecisionModal
        selected={selected}
        detail={detail}
        status={status}
        onClose={() => { setSelected(null); setDetail(null); }}
      />}

      {approving && <ApproveDrawer
        request={approving}
        status={status}
        portfolio={portfolio}
        busy={!!busy}
        onClose={() => setApproving(null)}
        onConfirm={approve}
      />}

      {confirm && <ConfirmModal request={confirm} busy={!!busy} onClose={() => setConfirm(null)}/>}
    </main>
  </ConfirmProvider>;
}
