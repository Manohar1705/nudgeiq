import { useState } from "react";
import ActionTab from "../insights/ActionTab";
import UpliftTab from "../insights/UpliftTab";
import UserTab from "../insights/UserTab";

const SUBTABS = ["Uplift Analytics", "Action Analytics", "User Analytics"] as const;

export default function InsightsPage() {
  const [tab, setTab] = useState<(typeof SUBTABS)[number]>(SUBTABS[0]);
  return (
    <>
      <div className="subtabs">
        {SUBTABS.map((t) => (
          <button key={t} className={t === tab ? "subtab active" : "subtab"} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>
      {tab === "Uplift Analytics" ? (
        <UpliftTab />
      ) : tab === "Action Analytics" ? (
        <ActionTab />
      ) : (
        <UserTab />
      )}
    </>
  );
}
