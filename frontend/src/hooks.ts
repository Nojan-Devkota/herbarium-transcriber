import { useCallback, useEffect, useRef, useState } from "react";

/** Fetch `load()` now and every `intervalMs` while `keepPolling(data)` is true. */
export function usePolling<T>(
  load: () => Promise<T>,
  intervalMs: number,
  keepPolling: (data: T | undefined) => boolean = () => true,
  restartKey: unknown = 0,
) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const loadRef = useRef(load);
  const keepRef = useRef(keepPolling);
  loadRef.current = load;
  keepRef.current = keepPolling;

  const refresh = useCallback(async () => {
    try {
      const next = await loadRef.current();
      setData(next);
      setError(undefined);
      return next;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return undefined;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      const next = await refresh();
      if (!cancelled && keepRef.current(next)) timer = window.setTimeout(tick, intervalMs);
    };
    tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [intervalMs, refresh, restartKey]);

  return { data, error, refresh, setData };
}
