# Tightened the workspace-vocabulary ratchet

`test_meta_ratchets.py`'s "workspace vocabulary in mngr-level code" ceiling
drops from 328 to 318: the comments and docstrings the docker-bridge latchkey
gateway route added to `mngr_latchkey` and `mngr_vps` say host / agent /
container instead, and a few existing lines were reworded along the way.
