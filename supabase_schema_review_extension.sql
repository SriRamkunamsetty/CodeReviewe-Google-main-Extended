-- ============================================================================
-- Review Engine Extension Schema
-- ============================================================================
-- Adds the two tables needed for Problem Statement 1 that don't fit the
-- existing `runs` table: `runs.repository_id` is NOT NULL and foreign-keys
-- to `repositories`, which assumes every run is tied to a tracked GitHub
-- repo. Ad-hoc code submissions have no repository, so rather than loosen
-- that constraint (and risk breaking the existing autonomous-SWE flow),
-- this adds two purpose-built tables and mirrors the existing org+user RLS
-- pattern used by `runs` / `logs` / `usage_events`.
--
-- Apply this after supabase_schema.sql. Idempotent (IF NOT EXISTS / OR
-- REPLACE throughout) so it's safe to re-run.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- historical_rules: the ingested CSV rows (id, type, description) per org
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS historical_rules (
    id BIGSERIAL PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    rule_ref TEXT NOT NULL,        -- the <id> column from the source CSV (kept as text; source ids aren't guaranteed numeric)
    type TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, rule_ref)
);

CREATE INDEX IF NOT EXISTS idx_historical_rules_org_id ON historical_rules(org_id);

ALTER TABLE historical_rules ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Org members can view their org's historical rules"
    ON historical_rules FOR SELECT
    USING (
        org_id IN (
            SELECT organization_id FROM organization_members
            WHERE user_id = auth.uid()
        )
    );

CREATE POLICY "Org members can ingest historical rules for their org"
    ON historical_rules FOR INSERT
    WITH CHECK (
        org_id IN (
            SELECT organization_id FROM organization_members
            WHERE user_id = auth.uid()
        )
    );

CREATE POLICY "Org members can update their org's historical rules"
    ON historical_rules FOR UPDATE
    USING (
        org_id IN (
            SELECT organization_id FROM organization_members
            WHERE user_id = auth.uid()
        )
    );

-- ----------------------------------------------------------------------------
-- review_sessions: one row per ad-hoc code submission ("Must have" #4.5)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_sessions (
    id BIGSERIAL PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL,  -- matches auth.uid() / runs.user_id convention elsewhere in this schema
    filename TEXT,
    language TEXT,
    -- Truncated/preview only — full submissions are not retained indefinitely
    -- by default to keep this table small; see code_length below for the
    -- true size. Raise this limit deliberately if full-code audit history
    -- is a requirement for your deployment.
    code_preview TEXT,
    code_length INTEGER,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    score NUMERIC(3, 1),                 -- overall 1.0-10.0
    rubric_breakdown JSONB,              -- RubricResult.to_dict()
    findings JSONB,                      -- merged static + LLM findings
    architecture_notes JSONB,
    optimization_notes JSONB,
    matched_historical_rules JSONB,
    static_analysis_available BOOLEAN,
    llm_parse_ok BOOLEAN,
    error_message TEXT,
    celery_task_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- This index is the one the growth-trend dashboard query hits directly:
-- "this user's sessions, newest first" (see /review/history in main.py).
CREATE INDEX IF NOT EXISTS idx_review_sessions_user_created
    ON review_sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_sessions_org_id ON review_sessions(org_id);

ALTER TABLE review_sessions ENABLE ROW LEVEL SECURITY;

-- Deliberately narrower than historical_rules: a user sees their OWN review
-- history, not their whole org's, so "growth tracked over time" reflects
-- one person's trend rather than a shared, noisier org-wide feed. Flip this
-- to the org_id-based policy (like historical_rules above) if you want
-- managers/leads to see the whole team's review history instead.
CREATE POLICY "Users can view their own review sessions"
    ON review_sessions FOR SELECT
    USING (user_id = auth.uid());

CREATE POLICY "Users can insert their own review sessions"
    ON review_sessions FOR INSERT
    WITH CHECK (user_id = auth.uid());

-- Service-role key (used by the Celery worker to update status/results) is
-- not subject to RLS at all, matching how `runs`/`logs` are updated
-- elsewhere in this codebase — no additional UPDATE policy is needed here
-- for the worker path. If you later add a user-facing "delete my review"
-- feature, add a scoped DELETE policy at that point rather than pre-emptively
-- here.

-- ----------------------------------------------------------------------------
-- usage_events: widen the existing CHECK constraint to accept review events
-- ----------------------------------------------------------------------------
-- usage_events.run_id is a foreign key into `runs`, which review_sessions
-- rows are NOT part of — do not pass a review_sessions.id as run_id when
-- recording these events (tasks.py's run_code_review_task deliberately
-- passes run_id=None for this reason). If you want per-review cost
-- attribution later, add a nullable `review_session_id` column here rather
-- than overloading `run_id`.
ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS usage_events_event_type_check;
ALTER TABLE usage_events ADD CONSTRAINT usage_events_event_type_check
    CHECK (event_type IN (
        'agent_run_started',
        'agent_run_completed',
        'agent_run_failed',
        'llm_tokens_consumed',
        'github_api_calls',
        'webhook_received',
        'ide_session_started',
        'ide_session_ended',
        'code_review_started',
        'code_review_completed',
        'code_review_failed'
    ));
