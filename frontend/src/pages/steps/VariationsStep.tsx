import { useState } from "react";
import { api } from "../../api/client";
import type { MessageVariant, ValidationIssue } from "../../api/types";
import { useWizard } from "../../wizard/WizardContext";
import { humanize } from "../../wizard/format";

export default function VariationsStep() {
  const { draft, ensureDraft, setVariant } = useWizard();
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<Record<string, ValidationIssue[]>>({});
  if (!draft) return null;
  const { variants, generation } = draft;
  const count = (s: string) => variants.filter((v) => v.status === s).length;

  // Every click is saved on the draft campaign straight away. A variant that is approved or
  // rejected here stays that way, even if the wizard is closed before launch.
  async function review(v: MessageVariant, action: "approve" | "reject"): Promise<boolean> {
    try {
      const id = await ensureDraft();
      const out = action === "approve" ? await api.approveVariant(id, v.variant_id) : await api.rejectVariant(id, v.variant_id);
      setVariant(out.variant);
      setWarnings((w) => ({ ...w, [v.variant_id]: out.warnings }));
      return true;
    } catch (e) {
      setErr(`${v.variant_id}: ${e instanceof Error ? e.message : String(e)}`);
      return false;
    }
  }

  async function one(v: MessageVariant, action: "approve" | "reject") {
    setBusy(v.variant_id);
    setErr(null);
    await review(v, action);
    setBusy(null);
  }

  async function approveAll() {
    setBusy("all");
    setErr(null);
    for (const v of variants.filter((x) => x.status === "pending")) {
      if (!(await review(v, "approve"))) break; // stop at the first problem and show it
    }
    setBusy(null);
  }

  return (
    <section>
      <div className="split">
        <div>
          <h2 className="step-title">Review and approve variations</h2>
          <p className="muted">{variants.length} variants for this template. Only approved ones can be sent.</p>
          <p className="counts">
            <span className="green">✓ {count("approved")} approved</span>{" "}
            <span className="red">✗ {count("rejected")} rejected</span>{" "}
            <span className="amber">{count("pending")} pending</span>
          </p>
          {generation && (
            <p className="small muted">
              Model {generation.model}: {generation.created.length} of {generation.requested} requested were added
              {generation.skipped.length > 0 && `, ${generation.skipped.length} skipped by the checks`}.
            </p>
          )}
        </div>
        <button className="btn soft" disabled={busy !== null || count("pending") === 0} onClick={approveAll}>
          {busy === "all" ? "Approving…" : "✓ Approve all pending"}
        </button>
      </div>

      {err && <p className="red">{err}</p>}
      {generation && generation.skipped.length > 0 && (
        <details className="small muted">
          <summary>Why {generation.skipped.length} draft(s) were skipped</summary>
          <ul>{generation.skipped.map((s, i) => <li key={i}>{s.code}: {s.message}</li>)}</ul>
        </details>
      )}

      <div className="stack">
        {variants.map((v, i) => (
          <div key={v.variant_id} className={`card variant ${v.status}`}>
            <div className="tpl-head">
              <span className="tag dark">V{i + 1}</span>
              <span className="tag">{humanize(v.principle)}</span>
              <span className="tag grey">{humanize(v.tone)}</span>
              {v.source === "llm" && <span className="tag grey">AI draft</span>}
              <span className={`pill ${v.status}`}>{humanize(v.status)}</span>
            </div>
            <pre className="mono">{v.text}</pre>
            {(warnings[v.variant_id] ?? []).map((w) => (
              <p key={w.code + w.path} className="small amber">{w.message}</p>
            ))}
            <div className="actions">
              <button className="btn approve" disabled={busy !== null || v.status !== "pending"} onClick={() => one(v, "approve")}>✓ Approve</button>
              <button className="btn reject" disabled={busy !== null || v.status === "rejected"} onClick={() => one(v, "reject")}>✗ Reject</button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
