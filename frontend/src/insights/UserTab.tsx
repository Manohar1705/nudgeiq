import { useState } from "react";
import CohortView from "./CohortView";
import CustomerView from "./CustomerView";

const VIEWS = ["Cohort learning", "Individual customer"] as const;

export default function UserTab() {
  const [view, setView] = useState<(typeof VIEWS)[number]>(VIEWS[0]);
  return (
    <div>
      <div className="subtabs inner">
        {VIEWS.map((v) => (
          <button key={v} className={v === view ? "subtab active" : "subtab"} onClick={() => setView(v)}>{v}</button>
        ))}
      </div>
      {view === "Cohort learning" ? <CohortView /> : <CustomerView />}
    </div>
  );
}
