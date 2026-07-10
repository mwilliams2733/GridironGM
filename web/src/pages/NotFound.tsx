import { Link } from "react-router-dom";

export function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-24 text-center">
      <div className="font-mono text-xs uppercase tracking-[0.2em] text-hash-500">4th & 404</div>
      <h1 className="font-display text-3xl font-semibold uppercase text-field-50">
        Route not found
      </h1>
      <p className="text-sm text-field-400">That page isn&apos;t on the roster.</p>
      <Link
        to="/"
        className="mt-2 rounded-md bg-hash-500 px-4 py-2 font-display text-xs font-semibold uppercase tracking-wide text-field-950 hover:bg-hash-600"
      >
        Back to Dashboard
      </Link>
    </div>
  );
}
