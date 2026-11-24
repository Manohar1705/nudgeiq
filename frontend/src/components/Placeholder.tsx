export default function Placeholder({ title, note }: { title: string; note: string }) {
  return (
    <div className="card placeholder">
      <h2>{title}</h2>
      <p className="muted">{note}</p>
    </div>
  );
}
