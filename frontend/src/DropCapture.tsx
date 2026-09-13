/* Drag a draft in anywhere, and it finds its production and scene.
 *
 * A global drop target mounted once at the app root. On drop, the file is
 * sent to the real backend's /api/identify, which parses it and matches it
 * against stored drafts by scene number and heading, the same alignment
 * anchor the rest of the pipeline rests on. No model call: scene numbers are
 * stable by industry convention, so a decisive match is a lookup, not a
 * judgment.
 *
 * A revision rarely lands as a whole draft; it lands one scene at a time, so
 * the reveal is staged: which production, then which scene. When neither
 * matches with confidence, the drop says so rather than guessing.
 */

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "./api";

export const DRAFT_DROPPED_EVENT = "bluepages:draft-dropped";

export interface DraftDroppedDetail {
  production: string;
  scene: string;
  filename: string;
}

type Phase = "idle" | "hover" | "reading" | "matched-production" | "matched-scene" | "unmatched";

export function DropCapture() {
  const navigate = useNavigate();
  const [phase, setPhase] = useState<Phase>("idle");
  const [filename, setFilename] = useState("");
  const [production, setProduction] = useState("");
  const [scene, setScene] = useState("");
  const dragDepth = useRef(0);

  useEffect(() => {
    const isFileDrag = (e: DragEvent) =>
      Array.from(e.dataTransfer?.types ?? []).includes("Files");

    const onDragEnter = (e: DragEvent) => {
      if (!isFileDrag(e)) return;
      e.preventDefault();
      dragDepth.current += 1;
      setPhase((p) => (p === "idle" ? "hover" : p));
    };

    const onDragOver = (e: DragEvent) => {
      if (!isFileDrag(e)) return;
      e.preventDefault();
    };

    const onDragLeave = (e: DragEvent) => {
      if (!isFileDrag(e)) return;
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (dragDepth.current === 0) {
        setPhase((p) => (p === "hover" ? "idle" : p));
      }
    };

    const onDrop = async (e: DragEvent) => {
      if (!isFileDrag(e)) return;
      e.preventDefault();
      dragDepth.current = 0;
      const file = e.dataTransfer?.files?.[0];
      if (!file) {
        setPhase("idle");
        return;
      }

      setFilename(file.name);
      setPhase("reading");

      let result;
      try {
        result = await api.identify(file);
      } catch {
        setPhase("unmatched");
        window.setTimeout(() => setPhase("idle"), 3000);
        return;
      }

      if (!result.production) {
        setPhase("unmatched");
        window.setTimeout(() => setPhase("idle"), 3000);
        return;
      }

      setProduction(result.production);
      setPhase("matched-production");

      await new Promise((resolve) => setTimeout(resolve, 900));

      if (result.scene) {
        setScene(result.scene);
        setPhase("matched-scene");
        await new Promise((resolve) => setTimeout(resolve, 900));
      }

      const params = new URLSearchParams(window.location.search);
      const onThisScene =
        window.location.pathname === "/dashboard" &&
        params.get("title") === result.production &&
        params.get("scene") === (result.scene ?? "");

      if (onThisScene) {
        // Already on the right console: no navigation needed, just kick off
        // the run in place. App.tsx is already mounted and listening.
        window.dispatchEvent(
          new CustomEvent<DraftDroppedDetail>(DRAFT_DROPPED_EVENT, {
            detail: { production: result.production, scene: result.scene ?? "", filename: file.name },
          }),
        );
        setPhase("idle");
      } else {
        // Navigating to a console that is not mounted yet, so an event
        // dispatched now would fire into a page that no longer exists by the
        // time the new one loads. ?run=1 carries that intent across instead.
        navigate(
          `/dashboard?title=${encodeURIComponent(result.production)}&scene=${encodeURIComponent(result.scene ?? "")}&run=1`,
        );
        setPhase("idle");
      }
    };

    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, [navigate]);

  if (phase === "idle") return null;

  return (
    <div className={`drop-overlay ${phase}`} aria-live="polite">
      <div className="drop-card">
        {phase === "hover" && (
          <>
            <span className="drop-icon" aria-hidden="true">
              ↓
            </span>
            <p>Drop the page. Bluepages will work out which scene it belongs to.</p>
          </>
        )}
        {phase === "reading" && (
          <>
            <span className="drop-spin" aria-hidden="true" />
            <p className="mono">{filename}</p>
            <p className="drop-sub">Analysing the script…</p>
          </>
        )}
        {phase === "matched-production" && (
          <>
            <span className="drop-spin" aria-hidden="true" />
            <p>
              Movie project identified: <strong>{production}</strong>
            </p>
            <p className="drop-sub">Identifying the scene…</p>
          </>
        )}
        {phase === "matched-scene" && (
          <>
            <span className="drop-check" aria-hidden="true">
              ✓
            </span>
            <p>
              Scene identified: <strong>{scene}</strong>
            </p>
            <p className="drop-sub">Opening {production}…</p>
          </>
        )}
        {phase === "unmatched" && (
          <>
            <span className="drop-icon" aria-hidden="true">
              ?
            </span>
            <p className="mono">{filename}</p>
            <p className="drop-sub">
              Could not match this page to a production on file. Upload it from the console
              instead.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
