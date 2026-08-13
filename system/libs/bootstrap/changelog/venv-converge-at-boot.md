The workspace venv is converged to the committed lockfile once at boot, before
supervisord starts anything, instead of being reconciled lazily by whichever
`uv run` happens to need it first. Removes the intermittent `ModuleNotFoundError`
that hit services importing during that rewrite window.
