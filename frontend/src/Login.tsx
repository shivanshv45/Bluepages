/* Sign in and sign up.
 *
 * The front door of the app, not a modal bolted onto the console. Same dark
 * ground and grain as the home page, so walking from one into the other
 * reads as the same product rather than a hand-off to a generic auth
 * template. One form with two modes, because the only difference between
 * signing in and signing up is a name field and which endpoint it posts to.
 *
 * Real accounts against the real backend: a hashed password, a session
 * token, and a login that rejects a wrong password. The reveal on success
 * holds on the confirmed state for a beat before handing off, the way the
 * console itself never jumps straight to a finished screen without showing
 * the work.
 */

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "./api";
import { FilmGrain } from "./FilmGrain";

type Phase = "form" | "checking" | "in";

export function Login() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"in" | "up">("in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [phase, setPhase] = useState<Phase>("form");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    setPhase("checking");
    try {
      await (mode === "in" ? api.signIn(email, password) : api.register(email, password, name));
      setPhase("in");
      window.setTimeout(() => navigate("/projects"), 1100);
    } catch (e) {
      const raw = (e as Error).message;
      const detail = raw.replace(/^\d+\s*/, "");
      try {
        setError(JSON.parse(detail).detail ?? detail);
      } catch {
        setError(detail || "Something went wrong. Try again.");
      }
      setPhase("form");
    }
  };

  return (
    <div className="login">
      <FilmGrain className="login-grain" />
      <div className="login-vignette" aria-hidden="true" />

      <div className={`login-stage phase-${phase}`}>
        <div className="wordmark login-mark">
          blue<span>pages</span>
        </div>

        {phase !== "in" && (
          <>
            <h1>{mode === "in" ? "Sign in" : "Create an account"}</h1>
            <p className="login-lede">
              {mode === "in"
                ? "Your productions, your revisions, your approvals."
                : "A production belongs to whoever ingests it."}
            </p>

            <form onSubmit={submit} className={phase === "checking" ? "busy" : ""}>
              {mode === "up" && (
                <label className="field">
                  <span>Name</span>
                  <input
                    className="text-input"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="First AD"
                    autoComplete="name"
                    disabled={phase === "checking"}
                  />
                </label>
              )}

              <label className="field">
                <span>Email</span>
                <input
                  className="text-input"
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@production.film"
                  autoComplete="email"
                  disabled={phase === "checking"}
                />
              </label>

              <label className="field">
                <span>Password</span>
                <input
                  className="text-input"
                  type="password"
                  required
                  minLength={8}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="At least 8 characters"
                  autoComplete={mode === "in" ? "current-password" : "new-password"}
                  disabled={phase === "checking"}
                />
              </label>

              {error && <div className="auth-error">{error}</div>}

              <button className="btn-solid login-submit" type="submit" disabled={phase === "checking"}>
                {phase === "checking" ? (
                  <>
                    <span className="login-spin" aria-hidden="true" />
                    Reading credentials…
                  </>
                ) : mode === "in" ? (
                  "Sign in"
                ) : (
                  "Create account"
                )}
              </button>
            </form>

            <button
              className="auth-switch"
              onClick={() => {
                setMode(mode === "in" ? "up" : "in");
                setError("");
              }}
              disabled={phase === "checking"}
            >
              {mode === "in" ? "No account? Create one." : "Already have an account? Sign in."}
            </button>
          </>
        )}

        {phase === "in" && (
          <div className="login-in">
            <span className="login-check" aria-hidden="true">
              ✓
            </span>
            <p>Signed in. Opening your productions…</p>
          </div>
        )}
      </div>
    </div>
  );
}
