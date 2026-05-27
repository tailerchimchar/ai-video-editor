import { Route, Routes } from "react-router-dom";
import { CompilationsList } from "@/pages/CompilationsList";
import { CompilationViewer } from "@/pages/CompilationViewer";

/**
 * Top-level route table.
 *
 * Two routes for Phase 1; designed to grow:
 *   /                          — compilations list
 *   /compilations/:id          — viewer + edit slider
 *
 * Later phases add `/assets`, `/transcripts`, `/intros`, etc. — each
 * is a peer route here; layout shell composes per-page.
 */
export function App() {
  return (
    <Routes>
      <Route path="/" element={<CompilationsList />} />
      <Route path="/compilations/:id" element={<CompilationViewer />} />
    </Routes>
  );
}
