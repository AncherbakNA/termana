"""Local opt-out for packaged update checks; no credentials or trust changes."""

import json
import os

from termana.storage.atomic_file import replace_file_atomically
from termana.storage.json_objects import reject_duplicate_json_keys
from termana.updates.manifest import UpdateError
from termana.updates.state import UpdateStateStore


class UpdatePreferencesStore(UpdateStateStore):
    """Reuse the checkpoint store's private directory/file and Windows ACL guards.

    A separate file keeps the network preference out of signed anti-rollback
    checkpoints. Invalid preferences disable automatic checks, not manual ones.
    """

    def __init__(self, directory):
        super().__init__(directory)
        self.path = directory / "preferences.json"

    def load_automatic(self):
        self._guard()
        try:
            try:
                descriptor = self._open_checkpoint()
            except FileNotFoundError:
                return True
            with os.fdopen(descriptor, "rb") as stream:
                before = self._file_security(stream.fileno())
                raw = stream.read(1025)
                if self._file_security(stream.fileno()) != before:
                    raise UpdateError("Update preference permissions changed")
            if len(raw) > 1024:
                raise ValueError()
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys)
            if (
                type(document) is not dict
                or set(document) != {"version", "automatic_checks"}
                or type(document["version"]) is not int
                or document["version"] != 1
                or type(document["automatic_checks"]) is not bool
            ):
                raise ValueError()
            return document["automatic_checks"]
        except UpdateError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise UpdateError("Update preferences are unreadable; automatic checks are disabled") from None

    def save_automatic(self, enabled):
        if type(enabled) is not bool:
            raise UpdateError("Choose whether to check for updates automatically")
        self.load_automatic()  # Do not replace corrupt or insecure existing state.
        try:
            replace_file_atomically(
                self.path, json.dumps({"version": 1, "automatic_checks": enabled}).encode("utf-8")
            )
        except OSError:
            raise UpdateError("Could not save the automatic update preference") from None
