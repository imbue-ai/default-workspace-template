-- A slice's pool_hosts row is now inserted BEFORE its VM is carved (status
-- 'baking'), so the bake's orphan reap sees the in-flight VM as tracked. The
-- bake result (agent id, forwarded ports, sshd host keys) lands on the row when
-- the bake finishes, so those columns must accept NULL until then.
ALTER TABLE pool_hosts ALTER COLUMN agent_id DROP NOT NULL;
