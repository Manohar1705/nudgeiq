export const WIZARD_STEPS = [
  "Cohort", "Context", "Options", "Rewards", "Templates", "Variations", "Launch",
] as const;

interface Props {
  current: number; // 0-based index of the active step
}

export default function Stepper({ current }: Props) {
  return (
    <ol className="stepper">
      {WIZARD_STEPS.map((label, i) => {
        const state = i < current ? "done" : i === current ? "active" : "todo";
        return (
          <li key={label} className={`step ${state}`}>
            <span className="step-dot">{state === "done" ? "✓" : i + 1}</span>
            <span className="step-label">{label}</span>
          </li>
        );
      })}
    </ol>
  );
}
