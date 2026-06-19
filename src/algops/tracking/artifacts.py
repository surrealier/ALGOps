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
        key = key.removeprefix(SCHEME).lstrip("/")
        return os.path.join(self.root, key.replace("/", os.sep))

    def put_bytes(self, key: str, data: bytes) -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return SCHEME + key.removeprefix(SCHEME).lstrip("/")

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
        return key.removeprefix(SCHEME).lstrip("/")

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
