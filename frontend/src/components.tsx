/* The pieces every screen is built from.
 *
 * There is essentially one: the ruled row. Scenes, findings, department notes,
 * run history and inventory are all that row with different content, which is
 * why learning one screen teaches the rest. See DESIGN.md.
 */

import type { ReactNode } from "react";

export function Sheet({
  title,
  sub,
  right,
  children,
}: {
  title?: string;
  sub?: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="sheet">
      {title && (
        <header className="sheet-head">
          <h2>{title}</h2>
          {sub && <span className="sub">{sub}</span>}
          {right && <div className="right">{right}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Row({
  id,
  children,
}: {
  id: ReactNode;
  children: ReactNode;
}) {
  // The "Sc." prefix is CSS, so the monospace cell holds only the number and
  // the numbers line up. A row with no scene drops the prefix.
  const noScene = id === "--" || id === "" || id == null;
  return (
    <div className="row">
      <div className="row-id" data-noscene={noScene ? "true" : undefined}>
        {id}
      </div>
      <div className="row-body">{children}</div>
    </div>
  );
}

export function Mark({
  kind = "plain",
  children,
}: {
  kind?: "plain" | "urgent" | "dept" | "ok" | "warn";
  children: ReactNode;
}) {
  return <span className={`mark ${kind}`}>{children}</span>;
}

export function Notice({ children }: { children: ReactNode }) {
  return <div className="notice">{children}</div>;
}

/* The scene sweep.
 *
 * Every scene as a cell, lighting up as the pipeline reaches it. On a
 * 120-scene feature this is the clearest possible picture of the scale of the
 * work, and it is truthful: each cell is one scene the agent actually read.
 */
export function Sweep({
  scenes,
  order,
}: {
  scenes: Record<string, string>;
  order: string[];
}) {
  if (!order.length) return null;
  return (
    <div className="sweep" role="img" aria-label={`${order.length} scenes processed`}>
      {order.map((number) => (
        <div key={number} className={`cell ${scenes[number] ?? "seen"}`} title={`Scene ${number}`}>
          {number}
        </div>
      ))}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="notice">{children}</div>;
}

export function timeOf(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "never";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 90) return "just now";
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/* Department labels.
 *
 * The API returns the stored key. These are the display names, matching the
 * fan-out's own spec table so an email and a screen never disagree about what
 * a department is called.
 */
const TITLES: Record<string, string> = {
  props: "Props",
  wardrobe: "Wardrobe",
  transport: "Transport",
  locations: "Locations",
  cast: "Cast",
  art: "Art",
  sfx: "SFX",
  stunts: "Stunts",
  schedule: "Schedule",
  clearance: "Clearance",
  ad: "AD",
  social: "Social",
  finance: "Finance",
};

export function departmentName(key: string): string {
  return TITLES[key] ?? key.replace(/_/g, " ");
}

/* Stored keys to readable words.
 *
 * `set_dressing` and `time_of_day_changed` are database vocabulary. A head of
 * department reads English.
 */
export function label(key: string): string {
  const words = key.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
