import { Route, Routes } from "react-router-dom";
import { AssetsList } from "@/pages/AssetsList";
import { CompilationsList } from "@/pages/CompilationsList";
import { CompilationViewer } from "@/pages/CompilationViewer";
import { NarrationPage } from "@/pages/NarrationPage";
import { VideosList } from "@/pages/VideosList";

/**
 * Top-level route table.
 *
 *   /                          — compilations list (rendered reels)
 *   /assets                    — sources gallery (raw recordings)
 *   /videos                    — every narratable video, each with a narrate link
 *   /compilations/:id         — viewer + per-clip editor
 *   /narrate/:kind/:key        — record voice/camera takes over a video, export
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
      <Route path="/videos" element={<VideosList />} />
      <Route path="/narrate/:kind/:key" element={<NarrationPage />} />
    </Routes>
  );
}
