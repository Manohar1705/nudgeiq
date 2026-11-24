// Decorative view only. The Master agent is a bandit, not a neural network,
// so nothing here is real weights. It just animates with the pipeline.

const LAYERS = [8, 7, 6, 5, 5];
const W = 600, H = 380, PAD_X = 50, PAD_Y = 30;

function layout() {
  return LAYERS.map((n, li) => {
    const x = PAD_X + (li * (W - 2 * PAD_X)) / (LAYERS.length - 1);
    return Array.from({ length: n }, (_, i) => ({ x, y: PAD_Y + ((i + 0.5) * (H - 2 * PAD_Y)) / n }));
  });
}

interface Props { active: boolean; chosenIndex: number | null }

export default function PolicyDiagram({ active, chosenIndex }: Props) {
  const layers = layout();
  const lines: JSX.Element[] = [];
  layers.slice(0, -1).forEach((from, li) =>
    from.forEach((a, i) =>
      layers[li + 1].forEach((b, j) =>
        lines.push(<line key={`${li}-${i}-${j}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={active ? "edge on" : "edge"} />),
      ),
    ),
  );
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="diagram" role="img" aria-label="Illustrative policy diagram">
      {lines}
      {layers.map((layer, li) =>
        layer.map((p, i) => {
          const isOut = li === layers.length - 1;
          const chosen = isOut && chosenIndex === i;
          return <circle key={`${li}-${i}`} cx={p.x} cy={p.y} r={chosen ? 11 : 7}
            className={chosen ? "node chosen" : active ? "node on" : "node"} />;
        }),
      )}
      <text x={PAD_X} y={H - 4} className="dlabel">State</text>
      <text x={W - PAD_X - 20} y={H - 4} className="dlabel">Scores</text>
    </svg>
  );
}
