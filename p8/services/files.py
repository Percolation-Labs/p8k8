"""Unified file reading and writing — local paths and S3 URIs."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from p8.settings import Settings


class FileService:
    """Read and write files from local paths or S3 URIs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._s3_client = None  # lazy boto3 init

    # ── Read ──────────────────────────────────────────────────────────────

    async def read(self, path: str) -> bytes:
        """Read file content. Dispatches based on URI scheme."""
        if path.startswith("s3://"):
            return await self._read_s3(path)
        return self._read_local(path)

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return (await self.read(path)).decode(encoding)

    def list_dir(self, path: str, pattern: str = "**/*.md") -> list[str]:
        """List files matching pattern. Local-only for now."""
        p = Path(path)
        if not p.is_dir():
            raise FileNotFoundError(f"Not a directory: {path}")
        return sorted(str(f) for f in p.glob(pattern) if f.is_file())

    # ── Write ─────────────────────────────────────────────────────────────

    async def write(self, path: str, data: bytes) -> str:
        """Write file content. Dispatches based on URI scheme. Returns the path/URI."""
        if path.startswith("s3://"):
            await self._write_s3(path, data)
            return path
        self._write_local(path, data)
        return path

    async def write_to_bucket(
        self, key: str, data: bytes, bucket: str | None = None
    ) -> str:
        """Write to the default (or specified) S3 bucket. Returns s3:// URI."""
        bucket = bucket or self.settings.s3_bucket
        if not bucket:
            raise ValueError("No S3 bucket configured (set P8_S3_BUCKET)")
        uri = f"s3://{bucket}/{key}"
        await self._write_s3(uri, data)
        return uri

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def mime_type_from_path(path: str) -> str:
        """Guess MIME type from file path. Falls back to application/octet-stream."""
        mt, _ = mimetypes.guess_type(path)
        return mt or "application/octet-stream"

    # ── Internal ──────────────────────────────────────────────────────────

    @staticmethod
    def _read_local(path: str) -> bytes:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return p.read_bytes()

    @staticmethod
    def _write_local(path: str, data: bytes) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def _ensure_s3_client(self):
        """Lazy-init shared boto3 S3 client."""
        if self._s3_client is None:
            import boto3

            kwargs = {}
            if self.settings.s3_region:
                kwargs["region_name"] = self.settings.s3_region
            if self.settings.s3_endpoint_url:
                kwargs["endpoint_url"] = self.settings.s3_endpoint_url
            self._s3_client = boto3.client("s3", **kwargs)

    @staticmethod
    def _parse_s3_uri(uri: str) -> tuple[str, str]:
        """Parse s3://bucket/key → (bucket, key)."""
        without_scheme = uri[5:]
        bucket, _, key = without_scheme.partition("/")
        if not key:
            raise ValueError(f"Invalid S3 URI (missing key): {uri}")
        return bucket, key

    async def _read_s3(self, uri: str) -> bytes:
        self._ensure_s3_client()
        bucket, key = self._parse_s3_uri(uri)

        def _get():
            resp = self._s3_client.get_object(Bucket=bucket, Key=key)
            return resp["Body"].read()

        return await asyncio.to_thread(_get)

    async def _write_s3(self, uri: str, data: bytes) -> None:
        self._ensure_s3_client()
        bucket, key = self._parse_s3_uri(uri)

        def _put():
            self._s3_client.put_object(Bucket=bucket, Key=key, Body=data)

        await asyncio.to_thread(_put)
