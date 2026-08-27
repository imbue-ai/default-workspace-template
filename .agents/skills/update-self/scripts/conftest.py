"""The skill's scripts import each other as siblings (the directory is ``sys.path[0]``
when ``update_self.py`` runs); put it there for the tests too."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
