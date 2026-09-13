/* The scene picker.
 *
 * A revision rarely lands as a whole new draft in a production's hands; it
 * lands scene by scene, the night before a call sheet locks. So the console
 * is not really about a draft, it is about one scene at a time: this is
 * scene 15, this is what changed, this is what it means. The project view
 * is the overview after the fact; opening one scene here is the real unit
 * of work, and it is what a dropped page resolves to as well (DropCapture).
 */

import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, type Production } from "./api";
import { Mark, Row } from "./components";

export function Scenes() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const title = params.get("title") ?? "";

  const [production, setProduction] = useState<Production | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!title) return;
    api
      .production(title)
      .then(setProduction)
      .catch((e: Error) => setError(e.message));
  }, [title]);

  const open = (number: string) => {
    navigate(`/dashboard?title=${encodeURIComponent(title)}&scene=${encodeURIComponent(number)}`);
  };

  return (
    <div className="scenes">
      <header className="scenes-nav">
        <button className="linkish" onClick={() => navigate("/projects")}>
          ← Productions
        </button>
        <span className="scenes-title mono">{title}</span>
        <span className="scenes-tag mono">select a scene</span>
      </header>

      {error && <div className="err banner">{error}</div>}

      {!production && !error && <div className="notice">Reading the scene list…</div>}

      {production && (
        <div className="scenes-list">
          {production.scenes.map((scene) => {
            const changeCount = production.changes.filter(
              (c) => c.scene_number === scene.number || c.from_scene === scene.number,
            ).length;
            return (
              <button key={scene.number} className="scene-row" onClick={() => open(scene.number)}>
                <Row id={scene.number}>
                  <div className="row-note">{scene.heading}</div>
                  <div className="row-meta">
                    {scene.state === "changed" && <Mark kind="warn">changed</Mark>}
                    {scene.state === "inserted" && <Mark kind="dept">inserted</Mark>}
                    {scene.state === "omitted" && <Mark kind="urgent">omitted</Mark>}
                    {scene.state === "seen" && <Mark>unchanged</Mark>}
                    {changeCount > 0 && (
                      <span className="hint">
                        {changeCount} finding{changeCount === 1 ? "" : "s"}
                      </span>
                    )}
                  </div>
                </Row>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
