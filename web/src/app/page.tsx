"use client";

import dynamic from "next/dynamic";

const Dashboard = dynamic(() => import("@/components/Dashboard"), {
  ssr: false,
  loading: () => <main className="app-shell" aria-busy="true">Loading dashboard…</main>,
});

export default function Home() {
  return <Dashboard />;
}
