"use client";

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import Dialog from "./Dialog";
import css from "./ConfirmModal.module.css";

export interface ConfirmRequest {
  title: string;
  message: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  /** When set, the user must type this word before confirming. */
  typeToConfirm?: string;
  /** When set, a free-text field is shown and its value is passed to onConfirm. */
  input?: { label: string; placeholder?: string; initial?: string; required?: boolean };
  onConfirm: (value?: string) => void | Promise<void>;
}

export default function ConfirmModal({ request, busy, onClose }: {
  request: ConfirmRequest;
  busy: boolean;
  onClose: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [value, setValue] = useState(request.input?.initial || "");

  const typedOk = !request.typeToConfirm || typed.trim().toUpperCase() === request.typeToConfirm;
  const inputOk = !request.input?.required || value.trim().length > 0;
  const ready = typedOk && inputOk && !busy;

  return <Dialog
    title={request.title}
    danger={request.danger}
    dismissable={!busy}
    size="sm"
    onClose={onClose}
    footer={<>
      <button className="button" data-dialog-cancel disabled={busy} onClick={onClose}>Cancel</button>
      <button
        className={`button ${request.danger ? "danger" : ""}`}
        disabled={!ready}
        onClick={() => void request.onConfirm(request.input ? value.trim() : undefined)}
      >{busy ? "Working…" : request.confirmLabel}</button>
    </>}
  >
    <form onSubmit={e => {
      e.preventDefault();
      if (ready && !request.danger) void request.onConfirm(request.input ? value.trim() : undefined);
    }}>
      <div className={`${css["confirm-body"]}`}>{request.message}</div>
      {request.input && <label className="confirm-type">
        <span>{request.input.label}</span>
        <input
          value={value}
          onChange={e => setValue(e.target.value)}
          placeholder={request.input.placeholder}
          aria-label={request.input.label}
          data-autofocus
        />
      </label>}
      {request.typeToConfirm && <label className="confirm-type">
        <span>Type <b>{request.typeToConfirm}</b> to confirm</span>
        <input
          value={typed}
          onChange={e => setTyped(e.target.value)}
          placeholder={request.typeToConfirm}
          aria-label={`Type ${request.typeToConfirm} to confirm`}
          autoComplete="off"
          spellCheck={false}
        />
      </label>}
    </form>
  </Dialog>;
}

/* ---- context: any panel can ask for a confirmation ---------------------------------- */

type Ask = (request: ConfirmRequest) => void;
const ConfirmContext = createContext<Ask>(() => {});

export function ConfirmProvider({ busy, children }: { busy: boolean; children: ReactNode }) {
  const [request, setRequest] = useState<ConfirmRequest | null>(null);
  const ask = useCallback<Ask>(r => setRequest({
    ...r,
    onConfirm: async (value?: string) => {
      setRequest(null);
      await r.onConfirm(value);
    },
  }), []);
  return <ConfirmContext.Provider value={ask}>
    {children}
    {request && <ConfirmModal request={request} busy={busy} onClose={() => setRequest(null)}/>}
  </ConfirmContext.Provider>;
}

export function useConfirm(): Ask {
  return useContext(ConfirmContext);
}
