"use client";
/**
 * Client-only shell controls. Theme state remains local to the browser and
 * does not affect upstream control-plane requests.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  ClipboardCheck,
  GitFork,
  Moon,
  ShieldAlert,
  Sun,
  Wrench,
} from "lucide-react";
import { useEffect, useState } from "react";
const links = [
  { href: "/", label: "Reliability Overview", icon: Activity },
  { href: "/incidents", label: "Incident Center", icon: ShieldAlert },
  { href: "/triage", label: "Triage Workbench", icon: Wrench },
  { href: "/approvals", label: "Approval Queue", icon: ClipboardCheck },
  { href: "/lineage", label: "Lineage & Blast Radius", icon: GitFork },
];
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const stored = localStorage.getItem("dq-theme");
    const enabled = stored
      ? stored === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    setDark(enabled);
    document.documentElement.dataset.theme = enabled ? "dark" : "light";
  }, []);
  function toggle() {
    const next = !dark;
    setDark(next);
    localStorage.setItem("dq-theme", next ? "dark" : "light");
    document.documentElement.dataset.theme = next ? "dark" : "light";
  }
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link href="/" className="brand">
          <span className="brand-mark">DQ</span>
          <span>
            <b>CONTROL PLANE</b>
            <small>evidence-led operations</small>
          </span>
        </Link>
        <nav aria-label="Primary navigation">
          {links.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              className={pathname === href ? "nav-link active" : "nav-link"}
            >
              <Icon size={18} aria-hidden="true" />
              {label}
            </Link>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span>Deterministic evidence is the source of truth.</span>
          <button
            className="icon-button"
            onClick={toggle}
            aria-label="Toggle color theme"
            aria-pressed={dark}
          >
            {dark ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>
      </aside>
      <main className="main-content">{children}</main>
    </div>
  );
}
