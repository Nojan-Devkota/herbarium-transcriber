import { useEffect, useState } from "react";
import Upload from "./pages/Upload";
import Review from "./pages/Review";
import Batch from "./pages/Batch";

type Route = { page: "upload" } | { page: "review"; id: number } | { page: "batch" };

function parseHash(hash: string): Route {
  const m = hash.match(/^#\/sheets\/(\d+)/);
  if (m) return { page: "review", id: Number(m[1]) };
  if (hash.startsWith("#/batch")) return { page: "batch" };
  return { page: "upload" };
}

export function navigate(path: string) {
  window.location.hash = path;
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));

  useEffect(() => {
    const onHash = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/">
          <span className="brand-mark" aria-hidden>
            ❦
          </span>
          Herbarium Label Transcriber
        </a>
        <nav>
          <a href="#/" className={route.page === "upload" ? "active" : ""}>
            Sheets
          </a>
          <a href="#/batch" className={route.page === "batch" ? "active" : ""}>
            Batch &amp; accuracy
          </a>
          <a href="/export.csv" className="export-link">
            Export Darwin Core CSV
          </a>
        </nav>
      </header>
      <main>
        {route.page === "upload" && <Upload />}
        {route.page === "review" && <Review key={route.id} sheetId={route.id} />}
        {route.page === "batch" && <Batch />}
      </main>
    </div>
  );
}
