"""Artifact store abstraction (CSV, run summaries, final YAML/report).

Dev backend writes to the local filesystem; prod uses MinIO/S3 in proxied-artifact mode
(slaves upload *through* the master so they never hold object-store credentials).
URIs are opaque ``algops://<key>`` strings stored in the tracking DB.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from algops.core import MasterSettings

SCHEME = "algops://"


class ArtifactKeyError(ValueError):
    """A key/URI that would escape the store (``..``, absolute paths, other schemes)."""


def clean_key(key_or_uri: str) -> str:
    """``algops://a/b`` or ``a/b`` -> ``a/b``; rejects anything that could leave the store root."""
    if not isinstance(key_or_uri, str):
        raise ArtifactKeyError("artifact key must be a string")
    key = key_or_uri
    if key.startswith(SCHEME):
        key = key[len(SCHEME):]
    elif "://" in key:
        raise ArtifactKeyError(f"not an ALGOps artifact uri: {key_or_uri!r}")
    key = key.replace("\\", "/").lstrip("/")
    parts = key.split("/")
    if not key or any(p in ("", ".", "..") for p in parts) or "\x00" in key or ":" in parts[0]:
        raise ArtifactKeyError(f"invalid artifact key: {key_or_uri!r}")
    return key


class ArtifactStore(ABC):
    @abstractmethod
    def put_bytes(self, key: str, data: bytes) -> str: ...

    @abstractmethod
    def get_bytes(self, uri: str) -> bytes: ...

    def put_text(self, key: str, text: str) -> str:
        return self.put_bytes(key, text.encode("utf-8"))

    def get_text(self, uri: str) -> str:
        return self.get_bytes(uri).decode("utf-8")


class LocalArtifactStore(ArtifactStore):
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key: str) -> str:
        key = clean_key(key)
        path = os.path.join(self.root, key.replace("/", os.sep))
        real_root = os.path.realpath(self.root)
        real = os.path.realpath(path)
        if real != real_root and not real.startswith(real_root + os.sep):
            raise ArtifactKeyError(f"artifact key escapes the store: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes) -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return SCHEME + clean_key(key)

    def get_bytes(self, uri: str) -> bytes:
        with open(self._path(uri), "rb") as fh:
            return fh.read()


class S3ArtifactStore(ArtifactStore):  # pragma: no cover - requires MinIO/S3 + 'cloud' extra
    """S3/MinIO-backed store (proxied-artifact mode: slaves upload through the master)."""

    def __init__(self, settings: MasterSettings) -> None:
        import boto3

        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
        )
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self.bucket)
            except Exception:
                pass

    def _key(self, key: str) -> str:
        return clean_key(key)

    def put_bytes(self, key: str, data: bytes) -> str:
        k = self._key(key)
        self._client.put_object(Bucket=self.bucket, Key=k, Body=data)
        return SCHEME + k

    def get_bytes(self, uri: str) -> bytes:
        return self._client.get_object(Bucket=self.bucket, Key=self._key(uri))["Body"].read()


def get_artifact_store(settings: MasterSettings | None = None) -> ArtifactStore:
    settings = settings or MasterSettings()
    if settings.artifact_backend == "s3":
        return S3ArtifactStore(settings)
    return LocalArtifactStore(settings.artifact_dir)
