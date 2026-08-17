# Uncertainties

Conflicts between documentation and code, noticed while writing specs; resolve and delete entries as they are fixed.

## mngr_minds_eval s3_store.py layout comment is stale

The layout comment at the top of `apps/mngr_minds_eval/imbue/mngr_minds_eval/s3_store.py` describes batch prefixes as `<eval_name>_<datetime>/`, but the code uses just `<name>/` (the launch name).
Noticed while writing `specs/minds-eval-harbor/concise.md`; the spec assumes the code is correct.
That spec deletes the old app in PR3, so fixing the comment is only worthwhile if that plan changes.
