import { useCallback, useEffect, useState } from "react";

/** One request's state: its data, its error, and a way to run it again. */
export function useApi<T>(load: () => Promise<T>): {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let live = true;
    load().then(
      (value) => {
        if (!live) return;
        setData(value);
        setError(null);
        setLoading(false);
      },
      (reason: Error) => {
        if (!live) return;
        setError(reason.message);
        setLoading(false);
      },
    );
    return () => {
      live = false;
    };
    // `load` is a fresh closure each render; `generation` is what re-runs it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generation]);

  const reload = useCallback(() => {
    setLoading(true);
    setGeneration((n) => n + 1);
  }, []);
  return { data, error, loading, reload };
}
