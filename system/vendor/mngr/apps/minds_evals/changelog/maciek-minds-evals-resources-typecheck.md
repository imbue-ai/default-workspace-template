`imbue/minds_evals/resources/` is type-checked again. Those files are shipped as source into the eval box and run there against the monorepo venv, importing `mngr_forward` and `litellm` -- packages this project deliberately does not depend on. Making this app a standalone uv project excluded it from the root `ty check` wholesale, and since the app's own `ty` config excludes `resources/` too (it cannot resolve those imports), the two files ended up checked by nothing at all.

The root workspace checks them now, which is also the accurate environment: the box runs that venv. A check in `imbue/minds_evals/test_ratchets.py` holds both halves in place -- this project must keep excluding `resources/`, and the root must keep not excluding it -- because nothing else fails when they are excluded in both places.

`imbue/minds_evals/templates/` remains unchecked in both, as it was before the split: rewardkit reaches the verifier container via `uvx` and exists in neither venv.
