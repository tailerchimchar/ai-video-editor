import { Route, Routes } from "react-router-dom";
import { AssetsList } from "@/pages/AssetsList";
import { CompilationsList } from "@/pages/CompilationsList";
import { CompilationViewer } from "@/pages/CompilationViewer";

/**
 * Top-level route table.
 *
 *   /                          — compilations list (rendered reels)
 *   /assets                    — sources gallery (raw recordings)
 *   /compilations/:id          — viewer + per-clip editor
 *
 * `/` and `/assets` are the two gallery views switched by
 * `GalleryTabs`. Future surfaces (`/transcripts`, `/intros`, …) slot in
 * as peer routes here.
 */
export function App() {
  return (
    <Routes>
      <Route path="/" element={<CompilationsList />} />
      <Route path="/assets" element={<AssetsList />} />
      <Route path="/compilations/:id" element={<CompilationViewer />} />
    </Routes>
  );
}
