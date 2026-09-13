/* The element inventory.
 *
 * Not a static table. Each element opens into its trail across drafts, which is
 * the whole point of tracking identity: the letter opener that moved from
 * scene 3 to scene 7 is one object with a history, not two unrelated rows.
 * A table that could not show that would be a list of nouns.
 */

import { useEffect, useState } from "react";
import { api, type InventoryRow, type TrailRow } from "./api";
import { Mark, Notice, Row, Sheet, departmentName, label } from "./components";

export function Inventory({ title }: { title: string }) {
  const [rows, setRows] = useState<InventoryRow[] | null>(null);
  const [open, setOpen] = useState<string>("");
  const [trail, setTrail] = useState<TrailRow[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!title) return;
    setRows(null);
    api
      .inventory(title)
      .then(setRows)
      .catch((e: Error) => setError(e.message));
  }, [title]);

  useEffect(() => {
    if (!open || !title) {
      setTrail(null);
      return;
    }
    api.trail(title, open).then(setTrail).catch(() => setTrail([]));
  }, [open, title]);

  if (error) {
    return (
      <Sheet title="Inventory">
        <div className="err">{error}</div>
      </Sheet>
    );
  }

  if (!rows) {
    // Not a shimmer. A word is honest and does not pretend to be content.
    return (
      <Sheet title="Inventory">
        <Notice>Reading the element database…</Notice>
      </Sheet>
    );
  }

  if (!rows.length) {
    return (
      <Sheet title="Inventory" sub="nothing recorded yet">
        <Notice>
          Every element the agent extracts is kept here with the scenes it
          appears in, so a production knows what it owns over time. It fills in
          as revisions are processed.
        </Notice>
      </Sheet>
    );
  }

  return (
    <Sheet
      title="Inventory"
      sub={`${rows.length} element${rows.length === 1 ? "" : "s"}`}
      right={<span className="hint">Select an element for its trail across drafts</span>}
    >
      <table>
        <thead>
          <tr>
            <th>element</th>
            <th>category</th>
            <th>department</th>
            <th className="num">scenes</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <RowPair
              key={`${row.name}:${row.category}`}
              row={row}
              open={open === row.name}
              trail={open === row.name ? trail : null}
              onToggle={() => setOpen(open === row.name ? "" : row.name)}
            />
          ))}
        </tbody>
      </table>
    </Sheet>
  );
}

function RowPair({
  row,
  open,
  trail,
  onToggle,
}: {
  row: InventoryRow;
  open: boolean;
  trail: TrailRow[] | null;
  onToggle: () => void;
}) {
  return (
    <>
      <tr
        onClick={onToggle}
        style={{ cursor: "pointer" }}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
        aria-expanded={open}
      >
        <td>
          {row.name}
          {row.branded ? (
            <>
              {" "}
              <Mark kind="warn">branded</Mark>
            </>
          ) : null}
        </td>
        <td className="hint">{label(row.category)}</td>
        <td>
          <Mark kind="dept">{departmentName(row.department)}</Mark>
        </td>
        <td className="num">{row.appearances}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={4} style={{ background: "var(--surface-2)", padding: 0 }}>
            <div className="rows">
              {trail === null ? (
                <div className="notice">Reading the trail…</div>
              ) : trail.length === 0 ? (
                <div className="notice">No recorded appearances.</div>
              ) : (
                trail.map((entry, i) => (
                  <Row key={i} id={entry.scene}>
                    <div className="row-note">
                      draft {entry.revision}
                      <span className="hint"> · {departmentName(entry.department)}</span>
                    </div>
                  </Row>
                ))
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
