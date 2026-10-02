"""Resolve secret refs (env:, k8s:, enc:) to values, once at provider construction, never per call."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from .secrets import decrypt_secret


class SecretResolver(Protocol):
    async def resolve(self, ref: str) -> str:
        """Returns the resolved secret value. Raises if the ref cannot be resolved."""
        ...


class EnvResolver:
    """ref = 'env:VAR_NAME' -> os.environ['VAR_NAME']."""

    async def resolve(self, ref: str) -> str:
        key = ref.removeprefix("env:")
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"EnvResolver: environment variable {key!r} is not set (ref={ref!r})")
        return value


class K8sFileResolver:
    """ref = 'k8s:namespace/secret-name' -> file at {mount_root}/{namespace}/{secret-name}."""

    def __init__(self, mount_root: str = "/var/run/secrets") -> None:
        self._mount_root = Path(mount_root)

    async def resolve(self, ref: str) -> str:
        path_part = ref.removeprefix("k8s:")
        secret_path = self._mount_root / path_part
        try:
            return secret_path.read_text().strip()
        except FileNotFoundError as exc:
            raise KeyError(
                f"K8sFileResolver: no secret file at {secret_path} (ref={ref!r})"
            ) from exc


class EncryptedResolver:
    """ref = 'enc:<fernet-token>' -> decrypted credential."""

    async def resolve(self, ref: str) -> str:
        return decrypt_secret(ref)


class CompositeSecretResolver:
    """Dispatches by ref prefix to the resolver that owns that scheme."""

    def __init__(self, k8s_mount_root: str = "/var/run/secrets") -> None:
        self._env = EnvResolver()
        self._k8s = K8sFileResolver(k8s_mount_root)
        self._enc = EncryptedResolver()

    async def resolve(self, ref: str) -> str:
        if ref.startswith("env:"):
            return await self._env.resolve(ref)
        if ref.startswith("k8s:"):
            return await self._k8s.resolve(ref)
        if ref.startswith("enc:"):
            return await self._enc.resolve(ref)
        # Never echo `ref`: it is most likely a raw API key pasted into the field.
        scheme = ref.split(":")[0] if ":" in ref else "<no scheme>"
        raise ValueError(
            f"unrecognized secret ref scheme (expected env:/k8s:/enc:), got {scheme[:12]!r}"
        )
