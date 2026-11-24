import { useState } from "react";
import { api } from "../../api/client";
import type { MessageTemplate } from "../../api/types";
import { useApi } from "../../hooks/useApi";
import { useWizard } from "../../wizard/WizardContext";
import { CHANNEL_LABELS } from "../../wizard/format";

const mergeVars = (body: string) => new Set(body.match(/\{\{\s*\w+\s*\}\}/g) ?? []).size;

export default function TemplatesStep() {
  const { draft, patch, setStep, ensureDraft } = useWizard();
  // The draft campaign is created here, the first time a step needs the server.
  const lib = useApi(async () => api.getTemplateLibrary(await ensureDraft()));
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!draft) return null;

  async function generate(t: MessageTemplate) {
    setBusy(t.template_id);
    setErr(null);
    try {
      const id = await ensureDraft();
      const generation = await api.generateVariants(id, t.template_id);
      const variants = await api.listVariants(id, t.template_id); // existing ones plus the new drafts
      patch({ templateId: t.template_id, templateChannel: t.channel, variants, generation });
      setStep(5);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const usable = (lib.data?.templates ?? []).filter((t) => draft.channels.includes(t.channel));

  return (
    <section>
      <h2 className="step-title">Base templates</h2>
      <p className="muted">Pick a base. NudgeIQ asks the language model for up to 5 new wordings and shows them with the variants this template already has, for you to review. Only channels chosen in step 3 are shown.</p>
      {lib.loading && <p className="muted">Loading templates…</p>}
      {lib.error && <p className="red">{lib.error}</p>}
      {err && <p className="red">{err}</p>}
      {lib.data && usable.length === 0 && <p className="muted">No templates match your selected channels.</p>}

      <div className="stack">
        {usable.map((t) => (
          <div key={t.template_id} className={draft.templateId === t.template_id ? "card tpl selected" : "card tpl"}>
            <div className="tpl-head">
              <b>{t.name}</b>
              <span className="tag">{CHANNEL_LABELS[t.channel] ?? t.channel}</span>
              <button className="link" disabled={busy !== null} onClick={() => generate(t)}>
                {busy === t.template_id ? "Generating…" : "Generate variations →"}
              </button>
            </div>
            <pre className="mono">{t.body}</pre>
            <span className="small muted">{mergeVars(t.body)} merge variables</span>
          </div>
        ))}
      </div>
    </section>
  );
}
