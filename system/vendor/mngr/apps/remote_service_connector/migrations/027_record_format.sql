-- Migration: workspace_records.record_format (forward-compatibility write-lock).
--
-- Stamps each synced workspace record with the semantic format it was
-- written at (existing rows and old-client pushes are format 1). Clients
-- whose supported format is below a record's treat it as read-only ("update
-- the app to manage this machine"), and the connector rejects a push whose
-- record_format is below the stored row's with a structured 409
-- (code: record_format_too_new) -- so an old client can never half-rewrite
-- record semantics it cannot see. The value only bumps on semantically
-- breaking record changes; additive display fields ride the
-- preserve-on-absent merge without a bump.

ALTER TABLE workspace_records
    ADD COLUMN record_format INTEGER NOT NULL DEFAULT 1;
