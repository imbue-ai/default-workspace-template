"""The last model and effort explicitly picked for each provider account.

Kept beside chat settings, separately from speed settings that older pages replace whole.
A preview writes its own copy; it never writes preferences into credential folders.
"""

import json
import os
import threading
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import TypeAdapter

from imbue.chat.models import ModelPick
from imbue.imbue_common.mutable_model import MutableModel

_PICKS = TypeAdapter(dict[str, ModelPick])


class ChatModelDefaultsStore(MutableModel):
    path: Path | None = Field(frozen=True)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _in_memory: dict[str, ModelPick] = PrivateAttr(default_factory=dict)

    def _read(self) -> dict[str, ModelPick]:
        if self.path is None:
            return dict(self._in_memory)
        try:
            return _PICKS.validate_json(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning("Could not read remembered chat models at {}: {}", self.path, error)
            return {}

    def get(self, account_id: str) -> ModelPick | None:
        with self._lock:
            return self._read().get(account_id)

    def remember(self, account_id: str, pick: ModelPick) -> None:
        with self._lock:
            picks = self._read()
            picks[account_id] = pick
            if self.path is None:
                self._in_memory = picks
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps({key: value.model_dump() for key, value in picks.items()}, indent=2) + "\n"
            )
            os.replace(temporary, self.path)
