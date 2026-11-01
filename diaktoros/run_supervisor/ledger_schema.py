"""The run ledger's schema (#1349): its tables, the host's migrations of ``runs``, and the
worker's check that a ledger is one the host prepared.

Split from ``run_supervisor/__init__.py``; the Supervisor reads these as ``ledger_schema.<name>``.
The host runs ``SCHEMA`` and ``migrate`` (``Supervisor`` with ``create``); a worker never
does, and refuses a ledger ``ledger_problem`` finds fault with.
"""
from __future__ import annotations

from contextlib import closing
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, delivery TEXT NOT NULL UNIQUE, repo TEXT NOT NULL,
 pr INTEGER NOT NULL, head TEXT NOT NULL, seat TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 owner TEXT, lease REAL, launch_intent REAL, pid INTEGER,
 outcome INTEGER, error TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_seat ON runs(seat,state);
CREATE INDEX IF NOT EXISTS runs_pr ON runs(repo,pr,state);

CREATE TABLE IF NOT EXISTS operator_notices (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), state TEXT NOT NULL,
 created REAL NOT NULL, delivered REAL
);
-- One adjudicator ruling per run, recorded BEFORE any notice or GitHub write.
-- notice: operator outbox (cron stdout) delivery; comment: optional PR comment.
-- shadow_run: the shadow review (shadow_reviews.run_id) the adjudicator was shown (#1112); ''
-- when it was shown none, NULL on a ruling from before this column (time rule). Kept out of
-- the statement: a comment inside it is stored in sqlite_master and breaks DROP COLUMN.
CREATE TABLE IF NOT EXISTS rulings (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), repo TEXT NOT NULL,
 pr INTEGER NOT NULL, head TEXT NOT NULL, turn_key TEXT NOT NULL,
 verdict TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
 observer TEXT NOT NULL DEFAULT 'pending', notice TEXT NOT NULL DEFAULT 'pending',
 notice_delivered REAL, comment TEXT NOT NULL DEFAULT 'pending',
 comment_id INTEGER, comment_error TEXT, updated REAL NOT NULL,
 shadow_run TEXT
);
-- The fixer's one answers comment per run (#52): 'posting' is committed before the POST, so a
-- lost response is 'uncertain' and never replayed. head is the pushed head it was posted at.
CREATE TABLE IF NOT EXISTS fixer_answers (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), repo TEXT NOT NULL, pr INTEGER NOT NULL,
 base TEXT NOT NULL, head TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL,
 comment_id INTEGER, error TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
-- A fixer's dispute (#401): every finding answered as not a defect, nothing pushed. One per
-- run; notice is the operator outbox state (pending -> sending -> delivered), claim-before-send.
CREATE TABLE IF NOT EXISTS disputes (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), repo TEXT NOT NULL, pr INTEGER NOT NULL,
 head TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
 notice TEXT NOT NULL DEFAULT 'pending', notice_delivered REAL,
 comment TEXT NOT NULL DEFAULT 'posting', comment_id INTEGER, comment_error TEXT
);
-- One issue triage per run (#213), recorded BEFORE the labels or comment are written. state:
-- recorded → posting → posted | uncertain (sent, outcome unknown: never replayed), or skipped
-- (a person labelled it first) / denied (authorization failed) / nothing (no label applied).
CREATE TABLE IF NOT EXISTS triage_results (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), repo TEXT NOT NULL, number INTEGER NOT NULL,
 labels TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, error TEXT,
 comment_id INTEGER, created REAL NOT NULL, updated REAL NOT NULL
);
-- One issue-fix write per run (#214), recorded BEFORE the branch push or the comment. kind:
-- 'pr' (push a new branch, open a PR, request review) or 'comment' (could not fix). state:
-- recorded → pushed → opened → requested, or posted (comment); denied before any write;
-- uncertain when a write's outcome is unknown (never replayed).
CREATE TABLE IF NOT EXISTS issue_fixes (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), repo TEXT NOT NULL, number INTEGER NOT NULL,
 base TEXT NOT NULL, kind TEXT NOT NULL, branch TEXT NOT NULL, state TEXT NOT NULL,
 new_head TEXT, pr_number INTEGER, comment_id INTEGER, error TEXT,
 created REAL NOT NULL, updated REAL NOT NULL
);
-- The host's proof of an issue fix's reproduction tests (#484): run on the original head and on
-- the fix, recorded with the fix's run before the write. proof is JSON (repro_proof).
CREATE TABLE IF NOT EXISTS repro_proofs (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), repo TEXT NOT NULL, number INTEGER NOT NULL,
 base TEXT NOT NULL, proof TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_receipts (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), state TEXT NOT NULL,
 generation TEXT NOT NULL, principal_id INTEGER NOT NULL,
 review_id INTEGER, verdict TEXT, created REAL NOT NULL, confirmed REAL
);
-- Issues a reviewer filed from its issue-tier findings (#247), recorded BEFORE the POST. One
-- title per PR (title_key, normalized), so a retry or a later round never files the same finding
-- twice; depth is the lineage: 1 for a finding on a person's PR, parent + 1 for a finding on a
-- PR that fixed a filed issue. state: recorded → posted (issue_number), denied, or uncertain.
CREATE TABLE IF NOT EXISTS filed_issues (
 run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL, repo TEXT NOT NULL,
 pr INTEGER NOT NULL, head TEXT NOT NULL, title TEXT NOT NULL, title_key TEXT NOT NULL,
 depth INTEGER NOT NULL, state TEXT NOT NULL, issue_number INTEGER, error TEXT,
 created REAL NOT NULL, updated REAL NOT NULL,
 PRIMARY KEY (run_id, seq), UNIQUE (repo, pr, title_key)
);
-- An issue a shadow review asked to file (#914): its broker is no-write, so the request is recorded
-- here, never posted by the seat. The host files it once the real review exists at that head and
-- the reviewer did not file the same finding (shadow_issues.file_pending), as the reader, labelled
-- shadow-found. state: recorded → posting → posted (issue_number), duplicate, uncertain or denied.
CREATE TABLE IF NOT EXISTS shadow_issues (
 run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL, repo TEXT NOT NULL,
 pr INTEGER NOT NULL, head TEXT NOT NULL, title TEXT NOT NULL, title_key TEXT NOT NULL,
 body TEXT NOT NULL, labels TEXT NOT NULL, state TEXT NOT NULL, issue_number INTEGER,
 error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 PRIMARY KEY (run_id, seq), UNIQUE (repo, pr, title_key)
);
-- An operator's explicit step on a PR (#678), recorded after it took effect, for the report:
-- retry (a run re-armed), escalate, review (a fresh review asked for), hold (a maintainer's
-- hold began). Only counted; nothing decides from it.
CREATE TABLE IF NOT EXISTS operator_actions (
 id INTEGER PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER NOT NULL, action TEXT NOT NULL,
 created REAL NOT NULL
);
-- A later bug linked to the approved PR that introduced it (#661), by the operator and verified
-- against GitHub when recorded: the PR merged, the issue was opened after the merge, and the
-- loop's reviewer approved the merged head (review_id). model is the resolved model of that
-- review's run, when the ledger has it. state: linked | retracted (kept, never deleted).
CREATE TABLE IF NOT EXISTS regressions (
 repo TEXT NOT NULL, issue INTEGER NOT NULL, pr INTEGER NOT NULL, head TEXT NOT NULL,
 review_id INTEGER NOT NULL, model TEXT, state TEXT NOT NULL, created REAL NOT NULL,
 updated REAL NOT NULL, PRIMARY KEY (repo, issue, pr)
);
-- A human overrode the loop (#220). kind merged_over_changes: the PR merged at a head where the
-- reviewer's latest live verdict was changes-requested. seat/model: the reviewer run that posted
-- that verdict (model NULL when the ledger has none); findings: the findings still open then (a
-- JSON list of {"id": "F1", "category": ...} objects, category "other" when none was recorded).
-- One row per (PR, head, kind). Only recorded and reported; nothing decides from it.
CREATE TABLE IF NOT EXISTS overrides (
 repo TEXT NOT NULL, pr INTEGER NOT NULL, head TEXT NOT NULL, kind TEXT NOT NULL,
 seat TEXT NOT NULL, model TEXT, review_id INTEGER NOT NULL, findings TEXT NOT NULL,
 merged_at REAL NOT NULL, created REAL NOT NULL, PRIMARY KEY (repo, pr, head, kind)
);
-- Facts about the ledger itself for the operator outbox, e.g. that it vanished and was
-- recreated empty. Delivered once by notify(), claim-before-send like every other notice.
CREATE TABLE IF NOT EXISTS ledger_events (
 id INTEGER PRIMARY KEY, message TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 created REAL NOT NULL, delivered REAL
);
-- A shadow review (#218): what a second reviewer model WOULD have submitted at a head the real
-- reviewer was queued for, recorded by the no-write broker and never posted. Compared with the
-- real review for the report; nothing in the loop decides from it. held: the human_paths reason
-- when the would-be APPROVE was left to a person (#859).
CREATE TABLE IF NOT EXISTS shadow_reviews (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), repo TEXT NOT NULL, pr INTEGER NOT NULL,
 head TEXT NOT NULL, model TEXT, verdict TEXT NOT NULL, body TEXT NOT NULL,
 authorized INTEGER NOT NULL, denial TEXT, held TEXT, created REAL NOT NULL
);
-- The adjudicator acting on its ACCEPT ruling at a PR head (#905): overruled is the one review
-- the ruling overruled, recorded with the approval; approve is the approval (its review_id),
-- dismiss the dismissal of that review. Each step: pending -> posting -> posted or uncertain,
-- then uncertain -> posted or denied only from what GitHub shows (never a second write); or
-- pending -> denied (error says why) or none (approve: approved by hand; dismiss: nothing left
-- to dismiss). posting is committed before the write, so a lost response is never replayed.
CREATE TABLE IF NOT EXISTS ruled_approvals (
 repo TEXT NOT NULL, pr INTEGER NOT NULL, head TEXT NOT NULL, overruled INTEGER,
 approve TEXT NOT NULL DEFAULT 'pending', review_id INTEGER,
 dismiss TEXT NOT NULL DEFAULT 'pending',
 error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 PRIMARY KEY (repo, pr, head)
);
"""
# The host's migrations of ``runs``, in order: (column, ALTER, follow-up statements). The one
# source for both the host (which applies what a ledger lacks) and the worker's schema check.
MIGRATIONS = (
    ("generation", "ALTER TABLE runs ADD COLUMN generation TEXT", ()),
    ("turn_key", "ALTER TABLE runs ADD COLUMN turn_key TEXT NOT NULL DEFAULT ''",
     ("DROP INDEX IF EXISTS runs_turn",
      "CREATE UNIQUE INDEX runs_turn ON runs(repo,pr,head,seat,turn_key)")),
    # Legacy rows cannot acquire permission from a later policy toggle.
    ("push_admitted", "ALTER TABLE runs ADD COLUMN push_admitted INTEGER NOT NULL DEFAULT 0", ()),
    ("push_intent", "ALTER TABLE runs ADD COLUMN push_intent REAL", ()),
    ("push_confirmed", "ALTER TABLE runs ADD COLUMN push_confirmed REAL", ()),
    # Issue #53: pre-write failure count, next retry time, bounded output tail.
    ("retries", "ALTER TABLE runs ADD COLUMN retries INTEGER NOT NULL DEFAULT 0", ()),
    ("retry_at", "ALTER TABLE runs ADD COLUMN retry_at REAL", ()),
    ("detail", "ALTER TABLE runs ADD COLUMN detail TEXT", ()),
    # The host dependency prefetch (#51): "fetching …" while it runs, then each ecosystem's
    # outcome. Host-written, one bounded line, never tool output.
    ("deps", "ALTER TABLE runs ADD COLUMN deps TEXT", ()),
    # Whether the seat was shown the whole change (#93, #110): '' when it was, the host's
    # reason when it was not, NULL before the change record was built. Written only by the
    # owning worker; the broker refuses an approval while it is set.
    ("partial_view", "ALTER TABLE runs ADD COLUMN partial_view TEXT", ()),
    # The turn's wall clock, fixed at enqueue (#49). NULL on legacy rows: the worker's own
    # child_timeout applies to them.
    ("budget", "ALTER TABLE runs ADD COLUMN budget REAL", ()),
    # When the latest attempt ended (``stats``): ``updated`` moves again on a re-arm or a
    # reconcile, so it is not a turn's end. NULL on rows that ended before this column.
    ("finished", "ALTER TABLE runs ADD COLUMN finished REAL", ()),
    # What a turn ran under (#472), written at launch: a hash of the prompt template, and the
    # model the seat's profile resolved to. NULL on rows from before: unknown.
    ("prompt_rev", "ALTER TABLE runs ADD COLUMN prompt_rev TEXT", ()),
    ("model", "ALTER TABLE runs ADD COLUMN model TEXT", ()),
    # The thinking level the turn ran at (#513), as the proxy saw its first model request
    # after the clamp: budget:<tokens>, effort:<level> or off. NULL: unknown.
    ("thinking", "ALTER TABLE runs ADD COLUMN thinking TEXT", ()),
    # What the turn's model calls used (#480), counted by the inference proxy: tokens in and out,
    # and the cost when the provider reported one. NULL: not recorded (rows from before, or a
    # turn that never reached the proxy); cost NULL: the provider reported none.
    ("tokens_in", "ALTER TABLE runs ADD COLUMN tokens_in INTEGER", ()),
    ("tokens_out", "ALTER TABLE runs ADD COLUMN tokens_out INTEGER", ()),
    ("cost", "ALTER TABLE runs ADD COLUMN cost REAL", ()),
    # An operator's `hermes dk cancel` of a launching or running turn (#1148): when it was asked.
    # The owning worker's heartbeat reads it and stops the sandbox; NULL: never asked.
    ("cancel_requested", "ALTER TABLE runs ADD COLUMN cancel_requested REAL", ()),
)


def migrate(con: sqlite3.Connection) -> None:
    """Add what an older ledger lacks, inside the host's open ``BEGIN IMMEDIATE``: the disputes
    and rulings columns, then every ``MIGRATIONS`` entry in order. A worker never calls it."""
    if 'comment' not in {r[1] for r in con.execute('PRAGMA table_info(disputes)')}:
        # A dispute from before this column has no recorded outcome: unknown.
        con.execute("ALTER TABLE disputes ADD COLUMN comment TEXT NOT NULL "
                    "DEFAULT 'uncertain'")
        con.execute('ALTER TABLE disputes ADD COLUMN comment_id INTEGER')
        con.execute('ALTER TABLE disputes ADD COLUMN comment_error TEXT')
    if 'shadow_run' not in {r[1] for r in con.execute('PRAGMA table_info(rulings)')}:
        con.execute('ALTER TABLE rulings ADD COLUMN shadow_run TEXT')   # #1112
    for column, alter, follow in MIGRATIONS:
        if column not in {r[1] for r in con.execute('PRAGMA table_info(runs)')}:
            con.execute(alter)
            for statement in follow:
                con.execute(statement)


_EXPECTED: dict[str, set[str]] = {}


def _expected_schema() -> dict[str, set[str]]:
    """Every table of a current ledger and its columns, from SCHEMA plus the migrations."""
    if not _EXPECTED:
        with closing(sqlite3.connect(":memory:")) as con:
            con.executescript(SCHEMA)
            for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                _EXPECTED[name] = {row[1] for row in con.execute(f"PRAGMA table_info({name})")}
        _EXPECTED["runs"] |= {column for column, _, _ in MIGRATIONS}
    return _EXPECTED


def ledger_problem(con: sqlite3.Connection) -> str | None:
    """Why ``con`` is not a review-loop ledger the host prepared, or None. Reads only."""
    for table, columns in _expected_schema().items():
        have = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if not have:
            return f"no {table} table"
        if columns - have:
            return f"{table} lacks {', '.join(sorted(columns - have))}"
    return None
