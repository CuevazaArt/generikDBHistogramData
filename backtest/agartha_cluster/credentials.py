"""Credentials manager.

Secrets are never stored in the cluster DB; only a **pointer** (service
name + username) lives in ``credentials_meta``. The actual API key and
secret are stored in the OS keyring (Windows CredentialManager / macOS
Keychain / Linux Secret Service) via the optional ``keyring`` package.

If ``keyring`` is not installed, ``EnvFileBackend`` is the documented
fallback: it reads from a 0600-mode file path supplied by the operator
(not auto-created). No plaintext credentials are ever written to the
repo or to DB rows.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from getpass import getpass
from typing import Optional

from backtest.agartha_cluster.cluster_db import ClusterDB

_DEFAULT_PROFILE = "default"
_DEFAULT_SERVICE = "binance_alpha"


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str
    profile: str = _DEFAULT_PROFILE
    service_name: str = _DEFAULT_SERVICE
    username: str = _DEFAULT_PROFILE


class CredentialsBackendError(RuntimeError):
    pass


class _KeyringBackend:
    """OS keyring backend (preferred). Lazy import."""

    storage_method = "os_keyring"

    def __init__(self):
        try:
            import keyring  # type: ignore
        except ImportError as e:  # pragma: no cover - depends on env
            raise CredentialsBackendError(
                "keyring package not installed. Run `pip install keyring` "
                "or use EnvFileBackend."
            ) from e
        self._keyring = keyring

    def _secret_key(self, service: str, username: str) -> str:
        return f"{service}:{username}:api_secret"

    def store(self, creds: Credentials) -> None:
        self._keyring.set_password(
            creds.service_name, creds.username, creds.api_key
        )
        self._keyring.set_password(
            self._secret_key(creds.service_name, creds.username),
            creds.username,
            creds.api_secret,
        )

    def load(self, *, service: str, username: str) -> Credentials | None:
        api_key = self._keyring.get_password(service, username)
        api_secret = self._keyring.get_password(self._secret_key(service, username), username)
        if not api_key or not api_secret:
            return None
        return Credentials(
            api_key=api_key,
            api_secret=api_secret,
            profile=username,
            service_name=service,
            username=username,
        )

    def delete(self, *, service: str, username: str) -> None:
        try:
            self._keyring.delete_password(service, username)
        except Exception:
            pass
        try:
            self._keyring.delete_password(self._secret_key(service, username), username)
        except Exception:
            pass


class _EnvFileBackend:
    """Fallback backend: read from ``BINANCE_ALPHA_KEY/SECRET`` env vars.

    The operator is expected to ``set`` them in a private shell session
    (PowerShell ``$env:`` or a 0600 .env file sourced before service
    start). Never written to disk by the cluster itself.
    """

    storage_method = "env_file"

    def store(self, creds: Credentials) -> None:
        raise CredentialsBackendError(
            "EnvFileBackend is read-only. Set BINANCE_ALPHA_KEY and "
            "BINANCE_ALPHA_SECRET in the environment before starting the service."
        )

    def load(self, *, service: str, username: str) -> Credentials | None:
        api_key = os.environ.get("BINANCE_ALPHA_KEY")
        api_secret = os.environ.get("BINANCE_ALPHA_SECRET")
        if not api_key or not api_secret:
            return None
        return Credentials(
            api_key=api_key,
            api_secret=api_secret,
            profile=username,
            service_name=service,
            username=username,
        )

    def delete(self, *, service: str, username: str) -> None:
        raise CredentialsBackendError("EnvFileBackend cannot delete env vars.")


def _select_backend(prefer: str | None = None):
    """Return the active backend. Prefer keyring; fallback to env."""
    if prefer == "env_file":
        return _EnvFileBackend()
    try:
        return _KeyringBackend()
    except CredentialsBackendError:
        return _EnvFileBackend()


def prompt_and_store(
    db: ClusterDB,
    *,
    profile: str = _DEFAULT_PROFILE,
    service_name: str = _DEFAULT_SERVICE,
    backend: str | None = None,
) -> Credentials:
    """Interactive: prompt for key/secret and store in keyring.

    Updates ``credentials_meta`` with the pointer. Never logs the secret.
    """
    bk = _select_backend(backend)
    if isinstance(bk, _EnvFileBackend):
        raise CredentialsBackendError(
            "Cannot prompt-and-store with EnvFileBackend. Install keyring "
            "(`pip install keyring`) or set BINANCE_ALPHA_KEY/SECRET env vars."
        )
    print(f"Provide Binance Alpha credentials for profile '{profile}':", flush=True)
    api_key = input("  API Key: ").strip()
    api_secret = getpass("  API Secret (hidden): ").strip()
    if not api_key or not api_secret:
        raise CredentialsBackendError("Empty key or secret; aborted.")
    creds = Credentials(
        api_key=api_key,
        api_secret=api_secret,
        profile=profile,
        service_name=service_name,
        username=profile,
    )
    bk.store(creds)
    db.upsert_credentials_meta(
        profile=profile,
        service_name=service_name,
        username=profile,
        storage_method=bk.storage_method,
    )
    print(f"Stored {profile} credentials in {bk.storage_method}.", flush=True)
    return creds


def load(
    db: ClusterDB,
    *,
    profile: str = _DEFAULT_PROFILE,
    backend: str | None = None,
) -> Credentials | None:
    """Load credentials following the meta pointer; None if not stored."""
    meta = db.get_credentials_meta(profile)
    if meta is None:
        # Try env fallback if no meta yet (handy for first-time CI / smoke).
        env_bk = _EnvFileBackend()
        return env_bk.load(service=_DEFAULT_SERVICE, username=profile)
    bk = _select_backend(backend or meta["storage_method"])
    creds = bk.load(service=meta["service_name"], username=meta["username"])
    if creds is not None:
        db.upsert_credentials_meta(
            profile=meta["profile"],
            service_name=meta["service_name"],
            username=meta["username"],
            storage_method=meta["storage_method"],
        )
    return creds


def delete(
    db: ClusterDB,
    *,
    profile: str = _DEFAULT_PROFILE,
    backend: str | None = None,
) -> None:
    """Remove credentials from keyring; meta row is kept (history)."""
    meta = db.get_credentials_meta(profile)
    if meta is None:
        return
    bk = _select_backend(backend or meta["storage_method"])
    bk.delete(service=meta["service_name"], username=meta["username"])


def ensure_credentials(
    db: ClusterDB,
    *,
    profile: str = _DEFAULT_PROFILE,
    interactive: bool = True,
) -> Credentials:
    """Return loaded credentials; prompt if not present and ``interactive``."""
    creds = load(db, profile=profile)
    if creds is not None:
        return creds
    if not interactive:
        raise CredentialsBackendError(
            f"Credentials for profile '{profile}' not found and interactive=False."
        )
    return prompt_and_store(db, profile=profile)
