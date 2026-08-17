-- The workspace's shell-service origin label (e.g. system_interface-<rand>),
-- recorded at share bring-up. The share stack deliberately routes only
-- <label>.<domain> origins on the relay (the bare workspace domain is
-- unrouted, shielding it from Certificate-Transparency scanners), so the
-- hosted web chrome needs one routable origin to enter and health-probe a
-- workspace; this column is where it learns it.
ALTER TABLE shares ADD COLUMN IF NOT EXISTS entry_label TEXT;
