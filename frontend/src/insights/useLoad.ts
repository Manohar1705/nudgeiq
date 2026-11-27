import { useEffect, useState } from "react";
import { ApiError } from "../api/client";

// A 404 means the endpoint is not on the backend yet; say so instead of showing a raw error.
export const explain = (e: unknown, what: string) =>
  e instanceof ApiError && e.status === 404
    ? `The backend has no ${what} endpoint yet.`
    : e instanceof Error ? e.message : String(e);

/**
 * Run `fetcher` on mount and again whenever `key` changes.
 * While reloading, the previous data stays in place (so Refresh does not flash empty);
 * callers that must not show stale data (a different customer) check `loading`.
 */
export function useLoad<T>(fetcher: () => Promise<T>, what: string, key: string | number = 0, pollMs = 0) {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>(
    { data: null, error: null, loading: true },
  );
  useEffect(() => {
    let alive = true;
    const run = (quiet: boolean) => {
      if (!quiet) setState((s) => ({ ...s, loading: true }));
      fetcher()
        .then((data) => alive && setState({ data, error: null, loading: false }))
        .catch((e: unknown) => alive && setState({ data: null, error: explain(e, what), loading: false }));
    };
    run(false);
    const timer = pollMs > 0 ? setInterval(() => run(true), pollMs) : undefined;
    return () => { alive = false; if (timer) clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, pollMs]);
  return state;
}
