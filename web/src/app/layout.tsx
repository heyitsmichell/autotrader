import type { Metadata } from "next";
import "../styles/tokens.css";
import "../styles/base.css";

export const metadata: Metadata = {
  title: "Trader | Market Intelligence",
  description: "Local AI-assisted trading dashboard",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body suppressHydrationWarning>{children}</body>
    </html>
  );
}
