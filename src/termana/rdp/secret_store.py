"""Endpoint/profile-bound sign-ins in the native RDP-only vault; no disk password."""
import json
import platform
from dataclasses import dataclass, field
from threading import Lock

from termana.credentials.secret_store import (
    RDP_KEYRING_SERVICE_NAME,
    KeyringSecretStore,
    SecretStoreError,
    _load_platform_backend,
)
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.models.rdp_options import RdpOptions
from termana.storage.json_objects import reject_duplicate_json_keys


def validate_password(password):
    try:
        if type(password) is not str or "\0" in password or len(password.encode("utf-8"))>4096:
            raise ValueError()
    except (UnicodeError, ValueError):
        raise SecretStoreError("The RDP password is outside the supported format or size") from None


@dataclass(frozen=True, slots=True)
class RdpLogin:
    username: str = field(repr=False)
    domain: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self):
        try:
            RdpOptions(username=self.username, domain=self.domain)
            validate_password(self.password)
            if not self.username or not self.password:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise SecretStoreError("The RDP sign-in is outside the supported format or size") from None


def _rdp_backend():
    backend = _load_platform_backend()
    system = platform.system()
    if system == "Linux":
        from termana.rdp.linux_vault import existing_collection_backend

        backend = existing_collection_backend(backend)
    elif system == "Windows":
        # RDP consent says "on this device", not roaming domain credentials.
        # Do not change the existing SSH namespace/backend's persistence policy.
        backend.persist = 2  # CRED_PERSIST_LOCAL_MACHINE.
    return backend


def _binding(profile):
    if type(profile) is not ConnectionProfile or profile.protocol is not ConnectionProtocol.RDP:
        raise SecretStoreError("A saved RDP profile is required")
    return dict(profile_id=str(profile.profile_id), host=profile.host, port=profile.port)


class RdpCredentialVault:
    def __init__(self, *, backend=None, backend_factory=None):
        self._state_lock, self._io_lock = Lock(), Lock()
        self._revoked = set()
        self._store = KeyringSecretStore(backend=backend, backend_factory=backend_factory or
                                        (None if backend is not None else _rdp_backend),
                                        service_name=RDP_KEYRING_SERVICE_NAME)

    def get_login(self, profile):
        with self._io_lock:
            if self.is_revoked(profile):
                return None
            login = self._get_login(profile)
            return None if self.is_revoked(profile) else login

    def _get_login(self, profile):
        expected = _binding(profile)
        try:
            raw = self._store.get_secret(profile.profile_id)
            if raw is None:
                return None
            if len(raw.encode("utf-8")) > 16384:
                raise ValueError()
            document = json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)
            if (type(document) is not dict or set(document) !=
                    {"schema_version", "profile_id", "host", "port", "username", "domain", "password"}
                    or type(document["schema_version"]) is not int or document["schema_version"] != 1
                    or type(document["port"]) is not int):
                raise ValueError()
            # Edited endpoint or foreign/copied profile never inherits a secret.
            if any(document[key] != value for key, value in expected.items()):
                return None
            login = RdpLogin(document["username"], document["domain"], document["password"])
            options = profile.rdp_options
            if ((options.username and options.username != login.username)
                    or (options.domain and options.domain != login.domain)):
                return None
            return login
        except Exception:
            raise SecretStoreError("The saved RDP sign-in could not be read safely") from None

    def save_login(self, profile, login):
        with self._io_lock:
            if self.is_revoked(profile):
                raise SecretStoreError("The saved RDP profile was removed")
            self._save_login(profile, login)

    def _save_login(self, profile, login):
        binding = _binding(profile)
        if type(login) is not RdpLogin:
            raise SecretStoreError("An RDP sign-in is required")
        document = dict(schema_version=1, **binding, username=login.username,
                        domain=login.domain, password=login.password)
        raw = json.dumps(document, ensure_ascii=True, separators=(",", ":"))
        try:
            if len(raw.encode("utf-8")) > 16384:
                raise ValueError()
            self._store.set_secret(profile.profile_id, raw)
        except Exception:
            raise SecretStoreError("The RDP sign-in could not be saved in the system vault") from None

    def forget_login(self, profile):
        _binding(profile)
        with self._io_lock:
            self._store.delete_secret(profile.profile_id)

    def revoke_profile(self, profile):
        """GUI-safe tombstone before queuing deletion; no OS I/O or lock wait on it."""
        _binding(profile)
        with self._state_lock:
            self._revoked.add(profile.profile_id)

    def is_revoked(self, profile):
        _binding(profile)
        with self._state_lock:
            return profile.profile_id in self._revoked
