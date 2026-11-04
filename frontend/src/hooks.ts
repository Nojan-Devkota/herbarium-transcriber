import { useCallback, useEffect, useRef, useState } from "react";

/** Milliseconds until the next poll given the latest data, or null to stop polling. */
export type PollInterval<T> = number | ((data: T | undefined) => number | null);

const RETRY_MS = 5000; // while the API is unreachable

/**
 * Fetch `load()` now, then again after `interval(data)` ms.
 *
 * - The interval is re-evaluated after every fetch, so pages can poll fast while
 *   work is in flight and slowly (or not at all) when everything is finished.
 * - Polling pauses while the browser tab is hidden and refreshes immediately when
 *   it becomes visible again.
 * - `refresh()` fetches now and reschedules, so a user action (e.g. an upload)
 *   switches back to fast polling without waiting for a slow timer.
 * - Changing `restartKey` restarts a stopped poll (e.g. after a re-run).
 */
export function usePolling<T>(load: () => Promise<T>, interval: PollInterval<T>, restartKey: unknown = 0) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const loadRef = useRef(load);
  const intervalRef = useRef(interval);
  const kickRef = useRef<() => Promise<T | undefined>>(async () => undefined);
  loadRef.current = load;
  intervalRef.current = interval;

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const fetchOnce = async (): Promise<T | undefined> => {
      try {
        const next = await loadRef.current();
        if (!cancelled) {
          setData(next);
          setError(undefined);
        }
        return next;
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
        return undefined;
      }
    };

    const schedule = (latest: T | undefined, failed: boolean) => {
      window.clearTimeout(timer);
      if (cancelled || document.hidden) return; // resumed by the visibility listener
      const iv = intervalRef.current;
      const ms = failed ? RETRY_MS : typeof iv === "function" ? iv(latest) : iv;
      if (ms !== null) timer = window.setTimeout(tick, ms);
    };

    const tick = async () => {
      const next = await fetchOnce();
      schedule(next, next === undefined);
      return next;
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
  return { data, error, refresh, setData };
}
