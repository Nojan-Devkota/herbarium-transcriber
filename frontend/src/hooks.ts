import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

/** Milliseconds until the next poll given the latest data, or null to stop polling. */
export type PollInterval<T> = number | ((data: T | undefined) => number | null);

const RETRY_MS = 5000; // while the API is unreachable or erroring (5xx / network)

/**
 * Fetch `load()` now, then again after `interval(data)` ms.
 *
 * - The interval is re-evaluated after every fetch, so pages poll fast while work is in
 *   flight and stop (null) when nothing can change.
 * - Polling pauses while the tab is hidden and refreshes once when it becomes visible.
 * - Only one request is in flight at a time; extra triggers are ignored.
 * - `refresh()` fetches now and reschedules, so a user action restarts fast polling.
 * - `setData()` (optimistic local edits) wins over any poll that was already in flight.
 * - 4xx errors (e.g. a deleted sheet) stop polling instead of retrying forever.
 * - Changing `restartKey` restarts a stopped poll (e.g. after a re-run).
 */
export function usePolling<T>(load: () => Promise<T>, interval: PollInterval<T>, restartKey: unknown = 0) {
  const [data, setDataState] = useState<T>();
  const [error, setError] = useState<string>();
  const loadRef = useRef(load);
  const intervalRef = useRef(interval);
  const kickRef = useRef<() => Promise<T | undefined>>(async () => undefined);
  const localVersion = useRef(0); // bumped by setData so stale in-flight polls are dropped
  loadRef.current = load;
  intervalRef.current = interval;

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let inFlight: Promise<T | undefined> | null = null;

    const schedule = (ms: number | null) => {
      window.clearTimeout(timer);
      if (!cancelled && !document.hidden && ms !== null) timer = window.setTimeout(tick, ms);
    };

    const tick = (): Promise<T | undefined> => {
      if (inFlight) return inFlight;
      const startedAt = localVersion.current;
      inFlight = (async () => {
        try {
          const next = await loadRef.current();
          if (cancelled) return undefined;
          if (startedAt === localVersion.current) {
            setDataState(next);
            setError(undefined);
          }
          const iv = intervalRef.current;
          schedule(typeof iv === "function" ? iv(next) : iv);
          return next;
        } catch (e) {
          if (cancelled) return undefined;
          setError(e instanceof Error ? e.message : String(e));
          const clientError = e instanceof ApiError && e.status >= 400 && e.status < 500;
          schedule(clientError ? null : RETRY_MS);
          return undefined;
        } finally {
          inFlight = null;
        }
      })();
      return inFlight;
    };

    const onVisibility = () => {
      if (!document.hidden) tick();
      else window.clearTimeout(timer);
    };

    kickRef.current = tick;
    document.addEventListener("visibilitychange", onVisibility);
    tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [restartKey]);

  const refresh = useCallback(() => kickRef.current(), []);
  const setData = useCallback((update: T | undefined | ((prev: T | undefined) => T | undefined)) => {
    localVersion.current += 1;
    setDataState(update as T | undefined);
  }, []);
  return { data, error, refresh, setData };
}
