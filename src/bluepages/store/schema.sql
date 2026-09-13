-- The element database: production -> draft -> scene -> element.
--
-- Written in the SQL subset both SQLite and Postgres accept, so the same
-- statements build the local development database and the Supabase one. Where
-- the two genuinely differ (identity columns, upsert syntax) the difference is
-- handled in Python rather than by keeping two schema files that drift apart.
--
-- The subtle table is `element_identity`. Everything else is a record of what a
-- draft said; that one is a claim about what is the *same thing* across drafts,
-- and it is what turns a diff tool into a system that knows a production's
-- inventory over time.

CREATE TABLE IF NOT EXISTS production (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft (
    id             TEXT PRIMARY KEY,
    production_id  TEXT NOT NULL REFERENCES production(id) ON DELETE CASCADE,
    -- Monotonic within a production. Draft 4 follows draft 3, and the diff
    -- between them is what a run records.
    revision       INTEGER NOT NULL,
    -- The industry colour: white, blue, pink, yellow, green. Free text because
    -- productions past green start again with double colours.
    colour         TEXT,
    source_path    TEXT,
    source_tier    TEXT NOT NULL,
    scene_count    INTEGER NOT NULL DEFAULT 0,
    ingested_at    TEXT NOT NULL,
    UNIQUE (production_id, revision)
);

CREATE TABLE IF NOT EXISTS scene (
    id             TEXT PRIMARY KEY,
    draft_id       TEXT NOT NULL REFERENCES draft(id) ON DELETE CASCADE,
    -- As written: "34", "34A". Stable across drafts by convention, which is
    -- why it is the alignment anchor and why it is indexed here.
    number         TEXT,
    heading        TEXT NOT NULL DEFAULT '',
    int_ext        TEXT,
    location       TEXT,
    time_of_day    TEXT,
    omitted        INTEGER NOT NULL DEFAULT 0,
    -- Position in the draft. Distinct from number: a scene can move without
    -- renumbering, and that is a scheduling fact.
    idx            INTEGER NOT NULL DEFAULT 0,
    UNIQUE (draft_id, number, idx)
);

CREATE INDEX IF NOT EXISTS scene_by_number ON scene (draft_id, number);

-- The same physical thing, tracked across drafts. Two element rows sharing an
-- identity_id are the same object appearing twice, which is what makes the
-- letter opener's trail through 3 -> 7 a queryable fact rather than a sentence
-- in a report.
CREATE TABLE IF NOT EXISTS element_identity (
    id             TEXT PRIMARY KEY,
    production_id  TEXT NOT NULL REFERENCES production(id) ON DELETE CASCADE,
    -- The canonical name, from the first draft that named it. Later drafts may
    -- word it differently; the identity is what says they are one object.
    name           TEXT NOT NULL,
    category       TEXT NOT NULL,
    department     TEXT NOT NULL,
    branded        INTEGER NOT NULL DEFAULT 0,
    first_draft_id TEXT REFERENCES draft(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS identity_by_production ON element_identity (production_id);

-- One element as a given draft's scene establishes it. This is a record of
-- what was said, not a claim about identity; that lives in element_identity.
CREATE TABLE IF NOT EXISTS element (
    id             TEXT PRIMARY KEY,
    scene_id       TEXT NOT NULL REFERENCES scene(id) ON DELETE CASCADE,
    identity_id    TEXT REFERENCES element_identity(id) ON DELETE SET NULL,
    name           TEXT NOT NULL,
    category       TEXT NOT NULL,
    department     TEXT NOT NULL,
    -- The line that establishes it. An element the AD cannot trace back to a
    -- line of script is one they cannot check.
    quote          TEXT NOT NULL DEFAULT '',
    branded        INTEGER NOT NULL DEFAULT 0,
    notes          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS element_by_scene ON element (scene_id);
CREATE INDEX IF NOT EXISTS element_by_identity ON element (identity_id);

-- One semantic finding: what changed between two drafts and what it means.
-- This is the product's output unit, persisted so a draft's result can be
-- queried against the previous draft's state.
CREATE TABLE IF NOT EXISTS change (
    id             TEXT PRIMARY KEY,
    production_id  TEXT NOT NULL REFERENCES production(id) ON DELETE CASCADE,
    from_draft_id  TEXT REFERENCES draft(id) ON DELETE CASCADE,
    to_draft_id    TEXT NOT NULL REFERENCES draft(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    summary        TEXT NOT NULL,
    reasoning      TEXT NOT NULL DEFAULT '',
    scene_number   TEXT,
    from_scene     TEXT,
    -- The element this change is about, when it is about one. Links a finding
    -- to the inventory, so "show me this prop's history" includes why it moved.
    identity_id    TEXT REFERENCES element_identity(id) ON DELETE SET NULL,
    confidence     REAL NOT NULL DEFAULT 0.0,
    risk           TEXT,
    -- Provenance: which model answered, and whether it was a fallback. Fallback
    -- output is weaker and that must stay visible after the run, not just during.
    model_name     TEXT,
    via_fallback   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS change_by_draft ON change (to_draft_id);
CREATE INDEX IF NOT EXISTS change_by_identity ON change (identity_id);

-- Department attribution, one row per department per change. A separate table
-- rather than a column because a change routes to several departments and each
-- one needs its own read state for the fan-out.
CREATE TABLE IF NOT EXISTS change_department (
    change_id      TEXT NOT NULL REFERENCES change(id) ON DELETE CASCADE,
    department     TEXT NOT NULL,
    PRIMARY KEY (change_id, department)
);

CREATE INDEX IF NOT EXISTS change_dept_by_department ON change_department (department);

-- One department's report on one draft: the fan-out, persisted. Separate from
-- `change` because a report is a department's phrasing of several changes, and
-- it is what actually gets sent in Layer 8.
CREATE TABLE IF NOT EXISTS report (
    id             TEXT PRIMARY KEY,
    to_draft_id    TEXT NOT NULL REFERENCES draft(id) ON DELETE CASCADE,
    department     TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    model_name     TEXT,
    via_fallback   INTEGER NOT NULL DEFAULT 0,
    -- Set when the AD approves the fan-out. Nothing is sent before this.
    approved_at    TEXT,
    sent_at        TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (to_draft_id, department)
);

CREATE INDEX IF NOT EXISTS report_by_draft ON report (to_draft_id);

-- One line item in a department's report.
CREATE TABLE IF NOT EXISTS report_note (
    id             TEXT PRIMARY KEY,
    report_id      TEXT NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    scene_number   TEXT,
    note           TEXT NOT NULL,
    action         TEXT NOT NULL DEFAULT '',
    urgent         INTEGER NOT NULL DEFAULT 0,
    idx            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS note_by_report ON report_note (report_id);

-- Who each department's report goes to. Per production, because the property
-- master on one show is not the property master on the next, and a global
-- address book would mail the wrong person on the second production.
--
-- A department with no recipient is not an error. It means nobody has been
-- added yet, and the report still exists to be read in the UI; sending is the
-- part that needs an address.
CREATE TABLE IF NOT EXISTS recipient (
    id             TEXT PRIMARY KEY,
    production_id  TEXT NOT NULL REFERENCES production(id) ON DELETE CASCADE,
    department     TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    email          TEXT NOT NULL,
    added_at       TEXT NOT NULL,
    UNIQUE (production_id, department, email)
);

CREATE INDEX IF NOT EXISTS recipient_by_production ON recipient (production_id, department);

-- One attempt to deliver one report to one address. Separate from `report`
-- because a report goes to several people and each can fail on its own, and
-- because "we tried and Resend rejected it" has to be distinguishable from
-- "nobody has been added yet". Without this a failed send looks like silence,
-- which is the same thing a department sees when a revision does not touch it.
CREATE TABLE IF NOT EXISTS delivery (
    id             TEXT PRIMARY KEY,
    report_id      TEXT NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    email          TEXT NOT NULL,
    status         TEXT NOT NULL,
    provider_id    TEXT,
    error          TEXT,
    attempted_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS delivery_by_report ON delivery (report_id);

-- One pipeline run. What was processed, what it cost, and whether it finished.
CREATE TABLE IF NOT EXISTS run (
    id             TEXT PRIMARY KEY,
    production_id  TEXT NOT NULL REFERENCES production(id) ON DELETE CASCADE,
    from_draft_id  TEXT REFERENCES draft(id) ON DELETE SET NULL,
    to_draft_id    TEXT REFERENCES draft(id) ON DELETE SET NULL,
    status         TEXT NOT NULL,
    scenes_parsed  INTEGER NOT NULL DEFAULT 0,
    findings       INTEGER NOT NULL DEFAULT 0,
    model_calls    INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    fallbacks      INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS run_by_production ON run (production_id, started_at);

-- --- accounts (Layer 10) --------------------------------------------------
--
-- A production belongs to whoever ingested it. Passwords are stored as a
-- PBKDF2 hash with a per-user salt, never in the clear.

CREATE TABLE IF NOT EXISTS account (
    id             TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL DEFAULT '',
    password_hash  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
    token          TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS session_by_account ON session (account_id);

-- Ownership is a join table rather than a column on production, because the
-- existing rows predate accounts and a NOT NULL column would break them.
CREATE TABLE IF NOT EXISTS production_owner (
    production_id  TEXT NOT NULL REFERENCES production(id) ON DELETE CASCADE,
    account_id     TEXT NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    role           TEXT NOT NULL DEFAULT 'owner',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (production_id, account_id)
);

-- --- the decision log (Layer 10) -----------------------------------------
--
-- What the agent decided, as opposed to what it observed. A finding is an
-- observation; a decision is the agent choosing to act on one: routing it to a
-- department, calling it urgent, drafting an email, holding it for approval.
--
-- `simulated` marks a decision the agent would take against a system it is not
-- actually connected to (a purchase order, a crew booking). Those are shown as
-- proposals, never as completed transactions, because claiming otherwise would
-- be a fabricated record.
CREATE TABLE IF NOT EXISTS decision (
    id             TEXT PRIMARY KEY,
    production_id  TEXT NOT NULL REFERENCES production(id) ON DELETE CASCADE,
    run_id         TEXT REFERENCES run(id) ON DELETE CASCADE,
    -- route | urgency | draft_email | await_approval | send | fallback | procure | social_post
    kind           TEXT NOT NULL,
    department     TEXT,
    scene_number   TEXT,
    -- What it decided, in one line.
    summary        TEXT NOT NULL,
    -- Why, in the agent's own words.
    rationale      TEXT NOT NULL DEFAULT '',
    -- What it did or proposes to do next.
    action         TEXT NOT NULL DEFAULT '',
    -- 0 real, 1 proposed against a system we are not connected to.
    simulated      INTEGER NOT NULL DEFAULT 0,
    -- pending | approved | rejected | done
    status         TEXT NOT NULL DEFAULT 'done',
    payload        TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS decision_by_production ON decision (production_id, created_at);

-- --- budget (Layer 10) ----------------------------------------------------
--
-- A production's department budgets, and what the agent's approved purchases
-- have drawn against them. The purchase is a proposal against a real vendor we
-- are not connected to, but the budget arithmetic is real: approving a
-- proposal commits its amount, and the remaining figure is computed from what
-- has actually been approved.

CREATE TABLE IF NOT EXISTS budget (
    id             TEXT PRIMARY KEY,
    production_id  TEXT NOT NULL REFERENCES production(id) ON DELETE CASCADE,
    department     TEXT NOT NULL,
    allocated      REAL NOT NULL DEFAULT 0,
    currency       TEXT NOT NULL DEFAULT 'USD',
    -- A procurement at or below this, and at or below what remains, clears on
    -- its own. Zero (the default) means nothing auto-approves.
    auto_approve   REAL NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL,
    UNIQUE (production_id, department)
);
