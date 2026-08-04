-- 0006_pipeline_run_parentage
-- Let one orchestrated flow own the ingestion runs it triggered.

-- ============================================================
-- Until now every entrypoint that wrote to pipeline_runs was a top-level CLI,
-- so a row and a run meant the same thing. The Prefect daily flow breaks that:
-- one flow run performs three ingestions and a dbt build, and both questions
-- below are worth answering from this table alone.
--
--   "Did last night's run succeed?"        -> the parent row
--   "Which source failed, and why?"        -> the child rows
--
-- A single flattened row with everything in `metadata` would answer the first
-- and reduce the second to string-digging inside JSONB. Separate rows with no
-- link would answer the second and force a correlation by timestamp to answer
-- the first — which is guesswork the moment two runs overlap or a retry lands.
--
-- Nullable, because it stays NULL for every run started directly from a CLI.
-- Those remain first-class: the flow is a convenience over the CLIs, not a
-- replacement for them, and a hand-run backfill must record itself exactly as
-- it always has.
--
-- Self-referencing FK rather than a bare UUID column, so a child can never
-- point at a parent that does not exist. ON DELETE CASCADE because a flow run
-- and its steps are one unit: deleting the parent while orphaning children
-- would leave rows whose status nothing explains.
-- ============================================================
ALTER TABLE public.pipeline_runs
    ADD COLUMN IF NOT EXISTS parent_run_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pipeline_runs_parent'
    ) THEN
        ALTER TABLE public.pipeline_runs
            ADD CONSTRAINT fk_pipeline_runs_parent
            FOREIGN KEY (parent_run_id)
            REFERENCES public.pipeline_runs (id)
            ON DELETE CASCADE;
    END IF;
END $$;

-- A row cannot be its own parent. Cheap, and it catches the specific bug of
-- passing a ledger its own run_id — which is easy to do when the parent and
-- child are opened by the same helper and reads as correct at the call site.
--
-- Note this does NOT prevent a longer cycle (A -> B -> A). Postgres cannot
-- express that as a CHECK, and it is not worth a trigger: parentage here is
-- exactly one level deep by construction, and assert_pipeline_run_parentage_is_one_level
-- would be the place to enforce it if the flow ever grew sub-flows.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_pipeline_runs_not_self_parent'
    ) THEN
        ALTER TABLE public.pipeline_runs
            ADD CONSTRAINT ck_pipeline_runs_not_self_parent
            CHECK (parent_run_id IS NULL OR parent_run_id <> id);
    END IF;
END $$;

-- Partial: the overwhelming majority of rows are top-level and have a NULL
-- parent, and the only query this index serves is "give me the children of
-- this flow run". Indexing the NULLs would be indexing the answer to a question
-- nobody asks.
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_parent
    ON public.pipeline_runs (parent_run_id)
    WHERE parent_run_id IS NOT NULL;
