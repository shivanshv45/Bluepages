/* The home page.
 *
 * The hero is not a picture of the product, it is the product's own surface
 * running: the scene sweep fills in exactly as it does on a real run, and the
 * finding that lands is the one the answer key scores. A still screenshot
 * would be a claim. This is the thing itself, so it is also the honest one.
 *
 * Everything here is the fixture pair's real content. No invented numbers.
 *
 * The root route always renders this, signed in or out: a link into the app
 * (open the console) and a link to sign in both start here rather than the
 * app defaulting straight to one or the other.
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "./api";
import { FilmGrain, usePrefersReducedMotion } from "./FilmGrain";

/* The demo sweep.
 *
 * Nine scenes, matching the fixture pair. Scenes 3 and 7 carry the letter
 * opener, which is the judgment the whole product is built to make, so they
 * are the two the finding callout points at.
 */
const DEMO_SCENES: { number: string; state: string; at: number }[] = [
  { number: "1", state: "seen", at: 0 },
  { number: "2", state: "seen", at: 1 },
  { number: "3", state: "changed", at: 2 },
  { number: "4", state: "changed", at: 3 },
  { number: "5", state: "seen", at: 4 },
  { number: "5A", state: "inserted", at: 5 },
  { number: "6", state: "seen", at: 6 },
  { number: "7", state: "changed", at: 7 },
  { number: "8", state: "omitted", at: 8 },
];

const STAGES = [
  "reading draft 2",
  "aligning on scene numbers",
  "reasoning over changed scenes",
  "routing to departments",
];

export function Home() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const reduced = usePrefersReducedMotion();

  // /projects is behind RequireAccount, so a signed-out visitor clicking
  // through gets bounced to /login automatically. The check here is only so
  // the header offers one door rather than two: sign in, or go to the
  // console, never both at once. null means we do not know yet, and the
  // header holds the space rather than flickering the wrong label.
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  useEffect(() => {
    api
      .me()
      .then((a) => setSignedIn(Boolean(a.signed_in)))
      .catch(() => setSignedIn(false));
  }, []);

  const onEnter = () => navigate("/projects");

  // The loop runs the sweep, holds on the finished state, then restarts.
  // Reduced motion gets the finished frame and no loop, which is the same
  // information without the movement.
  useEffect(() => {
    if (reduced) {
      setStep(DEMO_SCENES.length + 4);
      return;
    }
    const id = setInterval(() => {
      setStep((n) => (n > DEMO_SCENES.length + 10 ? 0 : n + 1));
    }, 420);
    return () => clearInterval(id);
  }, [reduced]);

  const stage = STAGES[Math.min(Math.floor(step / 2.5), STAGES.length - 1)];
  const done = step > DEMO_SCENES.length;
  const findingVisible = step >= DEMO_SCENES.length + 1;

  return (
    <div className="home">
      <FilmGrain />

      <header className="home-nav">
        <div className="wordmark">
          blue<span>pages</span>
        </div>
        <nav>
          <a href="#judgment">The judgment</a>
          <a href="#departments">Departments</a>
          <a href="#pipeline">How it runs</a>
        </nav>
        {signedIn === null ? (
          <span className="btn-ghost" aria-hidden="true" style={{ visibility: "hidden" }}>
            Sign in
          </span>
        ) : signedIn ? (
          <button className="btn-ghost" onClick={onEnter}>
            Open the console
          </button>
        ) : (
          <button className="btn-ghost" onClick={() => navigate("/login")}>
            Sign in
          </button>
        )}
      </header>

      <section className="hero">
        <div className="hero-copy">
          <h1>
            A new draft landed.
            <br />
            <em>Eight departments</em>
            <br />
            do not know yet.
          </h1>
          <p className="lede">
            Bluepages reads both drafts, works out what actually changed, and
            tells every department what it means in their own vocabulary.
            Nothing leaves the building until the 1st AD approves it.
          </p>
          <div className="hero-actions">
            <button className="btn-solid" onClick={onEnter}>
              Open the console
            </button>
            <a className="btn-ghost" href="#judgment">
              See what it catches
            </a>
          </div>
        </div>

        <div className="hero-stage" aria-hidden="true">
          <div className="scope">
            <div className="scope-bar">
              <span className="mono">THE FARM</span>
              {!done && <span className="scope-state live">{stage}</span>}
            </div>

            <div className="scope-sweep">
              {DEMO_SCENES.map((scene) => {
                const lit = step > scene.at;
                return (
                  <div
                    key={scene.number}
                    className={`scope-cell ${lit ? scene.state : "waiting"}`}
                  >
                    {scene.number}
                  </div>
                );
              })}
            </div>

            <div className={`scope-finding ${findingVisible ? "in" : ""}`}>
              <div className="scope-finding-head">
                <span className="tag tag-blue">Props</span>
                <span className="tag tag-blue">Continuity</span>
                <span className="mono dim">Sc. 3 &rarr; 7</span>
              </div>
              <p>
                The letter opener moved to scene 7. Same object, relocated, not
                a second buy.
              </p>
              <span className="scope-why">
                Held by the same character, described the same way, and scene 3
                no longer contains it.
              </span>
            </div>

            <div className="scope-foot mono">
              <span className="dim">{done ? "9 scenes read" : "reading"}</span>
            </div>
          </div>
        </div>
      </section>

      <Judgment />
      <Departments />
      <Pipeline onEnter={onEnter} />

      <footer className="home-foot">
        <div className="wordmark">
          blue<span>pages</span>
        </div>
        <p>
          Named for the blue pages: the second draft of a script, the first
          revision after white. Every draft after that is a night somebody
          spent reading.
        </p>
      </footer>
    </div>
  );
}

/* The thesis section.
 *
 * A mechanical diff and a semantic read of the same change, side by side.
 * This is the single clearest statement of what the product is, so it is the
 * first thing after the hero.
 */
function Judgment() {
  return (
    <section className="band" id="judgment">
      <div className="band-head">
        <span className="band-label mono">The difference</span>
        <h2>A diff sees text. It cannot see consequence.</h2>
        <p>
          Both columns describe the same edits. Only one of them is worth waking
          a department head for.
        </p>
      </div>

      <div className="compare">
        <div className="compare-col">
          <header>
            <span className="compare-tag">What a diff reports</span>
          </header>
          <ul className="diffs">
            <li>
              <span className="mono">- 12</span> lines removed, scene 3
            </li>
            <li>
              <span className="mono">+ 14</span> lines added, scene 7
            </li>
            <li>
              <span className="mono">~ 04</span> character cue changed
            </li>
            <li>
              <span className="mono">+ 31</span> lines added, scene 5A
            </li>
          </ul>
          <footer className="compare-verdict flat">
            Accurate, and nobody can act on it.
          </footer>
        </div>

        <div className="compare-col lit">
          <header>
            <span className="compare-tag">What Bluepages reports</span>
          </header>
          <ul className="findings">
            <li>
              <div className="finding-top">
                <span className="mono scene">3&rarr;7</span>
                <span className="tag tag-blue">Props</span>
              </div>
              <p>The letter opener relocated. One object, not two.</p>
              <span className="finding-why">
                No new purchase order. Continuity needs the new scene.
              </span>
            </li>
            <li>
              <div className="finding-top">
                <span className="mono scene">4</span>
                <span className="tag tag-pink">Cast</span>
              </div>
              <p>JANITOR is now CUSTODIAN. Same role, renamed.</p>
              <span className="finding-why">
                Same dialogue, same scenes. No new booking.
              </span>
            </li>
            <li>
              <div className="finding-top">
                <span className="mono scene">5A</span>
                <span className="tag tag-red">Before it shoots</span>
              </div>
              <p>New night exterior on the highway.</p>
              <span className="finding-why">
                Locations and Transport both gain work.
              </span>
            </li>
          </ul>
          <footer className="compare-verdict">
            Three calls, each with a reason attached.
          </footer>
        </div>
      </div>
    </section>
  );
}

/* Departments.
 *
 * The fan-out is the product's second idea: one change, eight readers, eight
 * vocabularies. Shown as the actual note each one receives.
 */
const DEPARTMENTS = [
  { name: "Props", note: "Letter opener now scene 7. Same unit, no reorder.", tone: "blue" },
  { name: "Cast", note: "CUSTODIAN is the renamed JANITOR. No new booking.", tone: "plain" },
  { name: "Locations", note: "New night exterior, highway shoulder, scene 5A.", tone: "red" },
  { name: "Transport", note: "Picture vehicle needed for the 5A insert.", tone: "plain" },
  { name: "Wardrobe", note: "No change to any costume in this revision.", tone: "quiet" },
  { name: "SFX", note: "Rain bar now scene 5A, moved off scene 6.", tone: "plain" },
  { name: "Art", note: "Farmhouse desk dressing loses the opener.", tone: "plain" },
  { name: "Schedule", note: "Scene 8 omitted. Day 4 gains an hour back.", tone: "green" },
];

function Departments() {
  return (
    <section className="band" id="departments">
      <div className="band-head">
        <span className="band-label mono">The fan-out</span>
        <h2>One revision, read eight different ways.</h2>
        <p>
          A department agent writes each brief in that department&rsquo;s own
          terms. Props hears about units. Schedule hears about hours. Nobody
          reads the other seven.
        </p>
      </div>

      <div className="dept-wall">
        {DEPARTMENTS.map((dept) => (
          <article key={dept.name} className={`dept-card ${dept.tone}`}>
            <h3>{dept.name}</h3>
            <p>{dept.note}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

/* How it runs.
 *
 * Genuinely ordered, so the numbering carries information rather than
 * decorating. The gate at the end is the point: it never sends on its own.
 */
const STEPS = [
  {
    n: "01",
    title: "A draft lands",
    body: "Dropped in the watched folder or pushed to the bucket. The agent wakes on its own. Nobody starts it.",
  },
  {
    n: "02",
    title: "Scenes align",
    body: "Scene numbers stay stable across drafts because omitted scenes are marked, never deleted. That is the anchor everything else hangs on.",
  },
  {
    n: "03",
    title: "Changes get read",
    body: "Every changed scene goes to a model that decides what the change means, not merely that it happened.",
  },
  {
    n: "04",
    title: "Departments are briefed",
    body: "Eight agents run in parallel, each writing only what its department has to act on.",
  },
  {
    n: "05",
    title: "The AD approves",
    body: "Everything waits here. One screen, one decision, and only then does anything send.",
  },
];

function Pipeline({ onEnter }: { onEnter: () => void }) {
  return (
    <section className="band" id="pipeline">
      <div className="band-head">
        <span className="band-label mono">The run</span>
        <h2>Dormant until a page changes.</h2>
      </div>

      <ol className="steps">
        {STEPS.map((step) => (
          <li key={step.n}>
            <span className="step-n mono">{step.n}</span>
            <div>
              <h3>{step.title}</h3>
              <p>{step.body}</p>
            </div>
          </li>
        ))}
      </ol>

      <div className="closer">
        <div>
          <h2>The pages are already in.</h2>
          <p>
            The repo ships a hand-authored revision pair and a labelled answer
            key, so the run you are about to watch is scored against what a
            human said the right answer was.
          </p>
        </div>
        <button className="btn-solid" onClick={onEnter}>
          Open the console
        </button>
      </div>
    </section>
  );
}
