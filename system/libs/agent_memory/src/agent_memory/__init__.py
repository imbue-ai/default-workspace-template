"""The agent's memory notes and the record of the changes an agent made to them.

Import the modules directly: ``agent_memory.changes`` is stdlib-only (Claude's hooks run it
under the bare ``python3``), while ``agent_memory.notes`` needs pydantic and PyYAML, so this
package's ``__init__`` imports neither.
"""
