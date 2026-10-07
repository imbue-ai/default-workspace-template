`memory_candidates.py` never offers an agent that is waiting on a background task (busy) as safe to stop; a candidate must be WAITING with nothing pending.
