"""System-keychain boundary with separate approved SSH/RDP namespaces."""

from __future__ import annotations

import importlib
import platform
from collections.abc import Callable
from typing import Final, Protocol
from uuid import UUID

KEYRING_SERVICE_NAME: Final = "io.github.ancherbakna.termana.ssh-password.v1"
RDP_KEYRING_SERVICE_NAME: Final = "io.github.ancherbakna.termana.rdp-password.v1"
MAX_SSH_PASSWORD_CHARACTERS: Final = 64 * 1024
_SUPPORTED_BACKENDS_BY_SYSTEM: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "Darwin": (("keyring.backends.macOS", "Keyring"),),
    "Windows": (("keyring.backends.Windows", "WinVaultKeyring"),),
    "Linux": (("keyring.backends.SecretService", "Keyring"),),
}
_SUPPORTED_BACKEND_NAMES: Final = frozenset(
    f"{module_name}.{class_name}"
    for platform_backends in _SUPPORTED_BACKENDS_BY_SYSTEM.values()
    for module_name, class_name in platform_backends
)


class SecretStore(Protocol):
    """Minimal injectable interface implemented by secure password stores."""

    def get_secret(self, credential_id: UUID) -> str | None:
        """Return one secret, or None when its keychain item is absent."""

    def set_secret(self, credential_id: UUID, password: str) -> None:
        """Create or replace one secret."""

    def delete_secret(self, credential_id: UUID) -> None:
        """Delete one secret, remaining idempotent when it is already absent."""


class _KeyringBackend(Protocol):
    """Structural subset shared by supported Python keyring backends."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class SecretStoreError(RuntimeError):
    """Raised when a system credential-store operation fails safely."""


class SecretStoreUnavailableError(SecretStoreError):
    """Raised when the platform has no supported secure credential backend."""


class KeyringSecretStore:
    """Store SSH passwords only in an approved native system keychain backend."""

    def __init__(
        self,
        *,
        backend: _KeyringBackend | None = None,
        backend_factory: Callable[[], _KeyringBackend] | None = None,
        service_name: str = KEYRING_SERVICE_NAME,
    ) -> None:
        """Inject a validated backend or defer native backend discovery until use."""
        if backend is not None and backend_factory is not None:
            raise ValueError("Provide either a keyring backend or a backend factory")
        if type(service_name) is not str or service_name not in (KEYRING_SERVICE_NAME, RDP_KEYRING_SERVICE_NAME):
            raise ValueError("Only reviewed Termana credential namespaces are allowed")
        self._service_name = service_name
        if backend is not None:
            _validate_backend_interface(backend)
        self._backend = backend
        self._backend_factory = backend_factory or _load_platform_backend

    def get_secret(self, credential_id: UUID) -> str | None:
        """Read a password without exposing backend-specific exceptions."""
        account = _account_for_credential(credential_id)
        try:
            password = self._get_backend().get_password(self._service_name, account)
        except SecretStoreUnavailableError:
            raise
        except Exception:
            raise SecretStoreError(
                "The system credential store could not read the saved password"
            ) from None
        if password is not None and (
            not isinstance(password, str) or len(password) > MAX_SSH_PASSWORD_CHARACTERS
        ):
            raise SecretStoreError("The system credential store returned an invalid password")
        return password

    def set_secret(self, credential_id: UUID, password: str) -> None:
        """Create or replace one password after validating only its type and bound."""
        account = _account_for_credential(credential_id)
        if not isinstance(password, str):
            raise SecretStoreError("SSH password must be text")
        if len(password) > MAX_SSH_PASSWORD_CHARACTERS:
            raise SecretStoreError("SSH password exceeds the supported size limit")
        try:
            self._get_backend().set_password(self._service_name, account, password)
        except SecretStoreUnavailableError:
            raise
        except Exception:
            raise SecretStoreError(
                "The system credential store could not save the password"
            ) from None

    def delete_secret(self, credential_id: UUID) -> None:
        """Remove one password without failing merely because it is already absent."""
        account = _account_for_credential(credential_id)
        try:
            backend = self._get_backend()
            if backend.get_password(self._service_name, account) is None:
                return
            backend.delete_password(self._service_name, account)
        except SecretStoreUnavailableError:
            raise
        except Exception:
            raise SecretStoreError(
                "The system credential store could not delete the saved password"
            ) from None

    def _get_backend(self) -> _KeyringBackend:
        """Initialize a native backend once, validating injected factories too."""
        if self._backend is not None:
            return self._backend
        try:
            backend = self._backend_factory()
            _validate_backend_interface(backend)
        except SecretStoreUnavailableError:
            raise
        except Exception:
            raise SecretStoreUnavailableError(
                "No supported secure system credential store is available"
            ) from None
        self._backend = backend
        return backend


def _load_platform_backend() -> _KeyringBackend:
    """Bypass keyring env/config/chaining and instantiate exact built-in backends."""
    backend_specs = _SUPPORTED_BACKENDS_BY_SYSTEM.get(platform.system())
    if backend_specs is None:
        raise SecretStoreUnavailableError(
            "No supported secure system credential store is available"
        )

    for module_name, class_name in backend_specs:
        try:
            module = importlib.import_module(module_name)
            backend_class = getattr(module, class_name)
            priority = backend_class.priority
            if isinstance(priority, bool) or not isinstance(priority, (int, float)):
                continue
            if priority <= 0:
                continue
            backend = backend_class()
            if type(backend) is not backend_class:
                continue
            _validate_supported_backend(backend)
            return backend
        except Exception:
            continue
    raise SecretStoreUnavailableError("No supported secure system credential store is available")


def _validate_supported_backend(backend: _KeyringBackend) -> None:
    """Reject fail/null/chained/third-party and plaintext keyring implementations."""
    backend_type = type(backend)
    qualified_name = f"{backend_type.__module__}.{backend_type.__name__}"
    if qualified_name not in _SUPPORTED_BACKEND_NAMES:
        raise SecretStoreUnavailableError(
            "The configured credential backend is not an approved system keychain"
        )
    _validate_backend_interface(backend)


def _validate_backend_interface(backend: _KeyringBackend) -> None:
    """Validate an explicitly injected test/application boundary structurally."""
    if any(
        not callable(getattr(backend, method_name, None))
        for method_name in ("get_password", "set_password", "delete_password")
    ):
        raise SecretStoreUnavailableError("The configured system credential backend is incomplete")


def _account_for_credential(credential_id: UUID) -> str:
    """Use an opaque canonical UUID instead of endpoint metadata as keychain account."""
    if not isinstance(credential_id, UUID):
        raise SecretStoreError("SSH credential ID must be a UUID")
    return str(credential_id)
