/** Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/ */
import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/app-shell";

export const metadata: Metadata = { title: "DQ Control Plane", description: "Evidence-first operations interface for Agentic Data Quality Triage." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en" suppressHydrationWarning><body><AppShell>{children}</AppShell></body></html>; }
