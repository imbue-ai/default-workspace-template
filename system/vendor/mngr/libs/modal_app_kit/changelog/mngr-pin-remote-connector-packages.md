Added `image.py`: the pinned image inputs shared by our Modal services -- the digest-pinned `python:3.12-slim-trixie` base (`PINNED_BASE_IMAGE`), the pinned in-build uv version, and `pinned_image(...)` which installs a committed export with `--require-hashes`. The pure export machinery (canonical `uv export` command, app registry, paths) lives in `imbue.imbue_common.modal_image_requirements` so the public mirror's minds deploy preflight can use it without this private package.

`testing.py` gained `export_image_requirements` / `regenerate_image_requirements`, shared by the per-app drift tests and the `just export-image-requirements` recipe.

The README's deployment model now documents the "every image input is pinned" rule (what is pinned, how it is enforced, and the known prisma-engine residual gap).
