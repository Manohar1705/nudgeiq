import { useEffect, useState } from "react";
import { useWizard } from "../../wizard/WizardContext";

export default function ContextStep() {
  const { config, draft, patch } = useWizard();
  const [brands, setBrands] = useState<{ id: string; name: string }[]>([]);
  const [products, setProducts] = useState<{ id: string; name: string; brand_id: string }[]>([]);
  const [promotions, setPromotions] = useState<{ id: string; name: string; discount_percentage: number | null }[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    const get = async (path: string) => {
      const r = await fetch(`/api/v1/${path}/`);
      if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
      return r.json();
    };
    Promise.all([get("brands"), get("products"), get("promotions")])
      .then(([b, p, pr]) => { setBrands(b); setProducts(p); setPromotions(pr); })
      .catch((e: unknown) => setLoadError(`Could not load Admin data: ${e instanceof Error ? e.message : String(e)}`));
  }, []);

  if (!config || !draft) return null;
  const total = config.context_signals.length;
  const on = Object.values(draft.signals).filter(Boolean).length;

  return (
    <section>
      <h2 className="step-title">Wire up the context the agent gets to see</h2>
      <p className="muted">{on} of {total} signals selected</p>
      <div className="grid cols-3">
        {config.context_signals.map((s) => {
          const checked = !!draft.signals[s.signal_id];
          return (
            <label key={s.signal_id} className={checked ? "pick selected" : "pick"}>
              <input
                type="checkbox"
                className="check"
                checked={checked}
                onChange={() => patch({ signals: { ...draft.signals, [s.signal_id]: !checked } })}
              />
              <span className="pick-title">{s.label}</span>
              <span className="pick-sub">{s.fields.length ? s.fields.join(", ") : "built-in"}</span>
            </label>
          );
        })}
      </div>

      <h3 className="step-title" style={{ marginTop: 24 }}>Business links (optional)</h3>
      <p className="muted">
        Pick the brand, product and promotion this campaign is for. Add new ones on the Admin tab.
      </p>
      {loadError && <p className="red">{loadError}</p>}
      <div className="grid cols-3">
        <label>
          <span className="pick-sub">Brand</span>
          <select
            value={draft.brandId}
            onChange={(e) => patch({ brandId: e.target.value, productId: "" })}
          >
            <option value="">None</option>
            {brands.map((b) => (
              <option key={b.id} value={b.id}>{b.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span className="pick-sub">Product</span>
          <select
            value={draft.productId}
            onChange={(e) => patch({ productId: e.target.value })}
            disabled={!draft.brandId}
          >
            <option value="">{draft.brandId ? "None" : "Select a brand first"}</option>
            {products
              .filter((p) => p.brand_id === draft.brandId)
              .map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
          </select>
        </label>
        <label>
          <span className="pick-sub">Promotion</span>
          <select
            value={draft.promotionId}
            onChange={(e) => patch({ promotionId: e.target.value })}
          >
            <option value="">None</option>
            {promotions.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}{p.discount_percentage != null ? ` (${p.discount_percentage}%)` : ""}
              </option>
            ))}
          </select>
        </label>
      </div>
    </section>
  );
}
