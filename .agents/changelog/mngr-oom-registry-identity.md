- After a test command is shed, the harden worker judges whether memory is recovering from the free memory the guard acts on (the first line of `memory_candidates.py`) instead of `/proc/meminfo`, which under a memory cap reads far more free memory than the guard sees.

- The update flow's memory-freeing guide describes the candidates' memory as their anonymous memory, roughly what stopping each frees.
