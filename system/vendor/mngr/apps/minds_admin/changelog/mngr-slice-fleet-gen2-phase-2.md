Slice-fleet-gen2 phase 2 (specs/slice-fleet-gen2):

- New `minds-admin server drain --server-id <id>`: marks a box `draining` (excluded from bake, restore candidates, and restart-in-place), destroys its unleased pool rows (so the connector's row-based lease cannot hand out new workspaces on it), and force-stops each leased workspace through the connector's admin stop -- the silent fleet-turnover primitive; restores land on the surviving boxes.
