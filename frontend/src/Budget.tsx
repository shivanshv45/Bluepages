/* Department budgets.
 *
 * The purchases are proposals, but the arithmetic is real: committed is the
 * sum of what has actually been approved, at-risk is what is still waiting on
 * a decision, and a department that would overrun says so.
 *
 * Editable, because the allocations start as defaults and a real production's
 * numbers are its own.
 */

import { useCallback, useEffect, useState } from "react";
import { api, type BudgetRow } from "./api";
import { departmentName } from "./components";

const money = (value: number) =>
  value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });

export function Budget({
  title,
  refreshKey,
}: {
  title: string;
  refreshKey: number;
}) {
  const [rows, setRows] = useState<BudgetRow[] | null>(null);
  const [editing, setEditing] = useState<string>("");
  const [draft, setDraft] = useState<string>("");
  const [editingLimit, setEditingLimit] = useState<string>("");
  const [limitDraft, setLimitDraft] = useState<string>("");

  const load = useCallback(() => {
    if (!title) return;
    api.budget(title).then(setRows).catch(() => setRows([]));
  }, [title]);

  useEffect(load, [load, refreshKey]);

  /* A triggered payment moves from pending to committed once Finance
     completes it, which happens on their own time rather than in the same
     tick as the approval. Poll gently while anything is still at risk, so
     that move shows up without a manual refresh. */
  useEffect(() => {
    if (!rows || !rows.some((r) => r.pending > 0)) return;
    const id = setInterval(load, 1500);
    return () => clearInterval(id);
  }, [rows, load]);

  const commit = async (department: string) => {
    const amount = Number(draft);
    setEditing("");
    if (!Number.isFinite(amount) || amount < 0) return;
    await api.setBudget(title, department, amount).catch(() => undefined);
    load();
  };

  /* Always resends the current allocation alongside the new limit: the
     backend's set_budget takes allocated as required, so a call that sent
     only auto_approve would zero the allocation out from under it. */
  const commitLimit = async (row: BudgetRow) => {
    const limit = Number(limitDraft);
    setEditingLimit("");
    if (!Number.isFinite(limit) || limit < 0) return;
    await api.setBudget(title, row.department, row.allocated, limit).catch(() => undefined);
    load();
  };

  if (!rows || !rows.length) return null;

  return (
    <div className="rail-block">
      <h3 className="rail-title">Budget</h3>
      {rows.map((row) => {
        // The bar shows committed against allocated, with at-risk stacked on
        // top so a pending purchase is visibly not yet spent.
        const spent = Math.min(100, (row.committed / (row.allocated || 1)) * 100);
        const risk = Math.min(100 - spent, (row.pending / (row.allocated || 1)) * 100);
        return (
          <div key={row.department} className="budget-row">
            <div className="budget-head">
              <span className="budget-dept">{departmentName(row.department)}</span>
              {editing === row.department ? (
                <input
                  className="budget-input mono"
                  autoFocus
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={() => commit(row.department)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commit(row.department);
                    if (e.key === "Escape") setEditing("");
                  }}
                />
              ) : (
                <button
                  className="budget-amount mono"
                  onClick={() => {
                    setEditing(row.department);
                    setDraft(String(row.allocated));
                  }}
                  title="Edit allocation"
                >
                  {money(row.remaining)}
                  <span className="budget-of"> / {money(row.allocated)}</span>
                </button>
              )}
            </div>
            <div className="budget-bar">
              <span className="spent" style={{ width: `${spent}%` }} />
              <span className="risk" style={{ width: `${risk}%` }} />
            </div>
            {(row.pending > 0 || row.over) && (
              <div className="budget-note">
                {row.over && <span className="over">over allocation</span>}
                {row.pending > 0 && (
                  <span className="risky">{money(row.pending)} at risk, not yet spent</span>
                )}
              </div>
            )}
            <div className="budget-note">
              {editingLimit === row.department ? (
                <input
                  className="budget-input mono"
                  autoFocus
                  value={limitDraft}
                  onChange={(e) => setLimitDraft(e.target.value)}
                  onBlur={() => commitLimit(row)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitLimit(row);
                    if (e.key === "Escape") setEditingLimit("");
                  }}
                />
              ) : (
                <button
                  className="linkish mono hint"
                  onClick={() => {
                    setEditingLimit(row.department);
                    setLimitDraft(String(row.auto_approve));
                  }}
                  title="Edit auto-approve limit"
                >
                  {row.auto_approve > 0
                    ? `auto-approves up to ${money(row.auto_approve)}`
                    : "auto-approve off"}
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
