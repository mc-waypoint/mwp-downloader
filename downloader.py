import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx


MAX_CONNECTIONS_PER_SERVER = 8
MAX_SHA1_RETRIES = 3
SHA1_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


class _HashUpdater(Protocol):
    def update(self, data: bytes, /) -> None: ...


@dataclass
class DownloadJob:
    url: str
    destination: Path
    sha1: str | None
    manifest: Path
    status: str = "queued"
    bytes_downloaded: int = 0
    total_bytes: int | None = None
    error: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _validate_url(url: str) -> None:
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError(f"不支持的下载 URL: {url}")
    try:
        parsed_url.port
    except ValueError as error:
        raise ValueError(f"下载 URL 端口无效: {url}") from error


def _resolve_destination(path: str) -> Path:
    destination = Path(path)
    if not destination.is_absolute():
        destination = Path(__file__).resolve().parent / destination
    return destination.resolve()


def create_manual_job(url: str, path: str) -> DownloadJob:
    url = url.strip()
    path = path.strip()
    if not url:
        raise ValueError("请输入下载 URL")
    if not path:
        raise ValueError("请输入保存路径")
    _validate_url(url)
    return DownloadJob(url, _resolve_destination(path), None, Path("<manual>"))


def _job_from_entry(url: object, details: object, manifest: Path) -> DownloadJob:
    if not isinstance(url, str) or not isinstance(details, dict):
        raise ValueError("每个下载项都必须是 URL 对应的对象")
    _validate_url(url)

    path = details.get("path")
    sha1 = details.get("sha1")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"下载项 {url} 缺少有效的 path")
    if sha1 is None or sha1 == "":
        normalized_sha1 = None
    elif isinstance(sha1, str) and SHA1_PATTERN.fullmatch(sha1):
        normalized_sha1 = sha1.lower()
    else:
        raise ValueError(f"下载项 {url} 的 sha1 必须是 40 位十六进制字符串")

    return DownloadJob(url, _resolve_destination(path), normalized_sha1, manifest)


def parse_manifest(manifest: Path) -> list[DownloadJob]:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取清单 {manifest.name}: {error}") from error

    if isinstance(data, list):
        entries = []
        for item in data:
            if not isinstance(item, dict) or "url" not in item:
                raise ValueError(f"{manifest.name} 中的数组项必须包含 url、path 和 sha1")
            entries.append((item["url"], item))
    elif isinstance(data, dict) and isinstance(data.get("url"), str):
        entries = [(data["url"], data)]
    elif isinstance(data, dict):
        entries = list(data.items())
    else:
        raise ValueError(f"{manifest.name} 必须是下载项数组或 URL 到下载信息的对象")

    if not entries:
        raise ValueError(f"{manifest.name} 中没有下载项")

    jobs = [_job_from_entry(url, details, manifest) for url, details in entries]
    destinations = [job.destination for job in jobs]
    if len(set(destinations)) != len(destinations):
        raise ValueError(f"{manifest.name} 中存在重复的保存路径")
    return jobs


def _update_sha1(path: Path, digest: _HashUpdater) -> None:
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)


def _file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    _update_sha1(path, digest)
    return digest.hexdigest()


class DownloadManager:
    def __init__(self) -> None:
        self._server_semaphores: dict[str, asyncio.Semaphore] = {}

    def _semaphore_for(self, url: str) -> asyncio.Semaphore:
        parsed_url = urlsplit(url)
        hostname = parsed_url.hostname
        if hostname is None:
            raise ValueError(f"无效的下载服务器 URL: {url}")
        server = hostname.casefold().rstrip(".")
        if server not in self._server_semaphores:
            self._server_semaphores[server] = asyncio.Semaphore(MAX_CONNECTIONS_PER_SERVER)
        return self._server_semaphores[server]

    async def download(self, job: DownloadJob, client: httpx.AsyncClient) -> None:
        async with job._lock:
            semaphore = self._semaphore_for(job.url)
            async with semaphore:
                try:
                    await self._download(job, client)
                except (httpx.HTTPError, OSError, ValueError) as error:
                    job.status = "failed"
                    job.error = str(error)

    async def _download(self, job: DownloadJob, client: httpx.AsyncClient) -> None:
        job.status = "checking"
        job.destination.parent.mkdir(parents=True, exist_ok=True)
        partial = job.destination.with_name(job.destination.name + ".part")

        if job.destination.is_file():
            existing_sha1 = await asyncio.to_thread(_file_sha1, job.destination)
            if existing_sha1 == job.sha1:
                job.status = "completed"
                job.bytes_downloaded = job.destination.stat().st_size
                job.total_bytes = job.bytes_downloaded
                return

        for attempt in range(MAX_SHA1_RETRIES + 1):
            offset = partial.stat().st_size if partial.is_file() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            digest = hashlib.sha1()
            if offset:
                await asyncio.to_thread(_update_sha1, partial, digest)

            async with client.stream("GET", job.url, headers=headers) as response:
                if offset and response.status_code == 416:
                    if job.sha1 is not None and digest.hexdigest() == job.sha1:
                        os.replace(partial, job.destination)
                        job.status = "completed"
                        job.bytes_downloaded = offset
                        job.total_bytes = offset
                        return
                    partial.unlink(missing_ok=True)
                    if attempt < MAX_SHA1_RETRIES:
                        continue
                    if job.sha1 is not None:
                        job.status = "skipped"
                        job.error = None
                        return
                    response.raise_for_status()

                response.raise_for_status()
                resumed = offset > 0 and response.status_code == 206
                if resumed:
                    content_range = response.headers.get("content-range", "")
                    if not content_range.startswith(f"bytes {offset}-"):
                        raise ValueError("服务器返回了无效的 Content-Range")
                else:
                    offset = 0
                    digest = hashlib.sha1()

                content_length = response.headers.get("content-length")
                job.total_bytes = offset + int(content_length) if content_length else None
                job.bytes_downloaded = offset
                job.status = "downloading"
                mode = "ab" if resumed else "wb"
                with partial.open(mode) as file:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if not chunk:
                            continue
                        file.write(chunk)
                        digest.update(chunk)
                        job.bytes_downloaded += len(chunk)

            if job.sha1 is not None and digest.hexdigest() != job.sha1:
                if attempt < MAX_SHA1_RETRIES:
                    partial.unlink(missing_ok=True)
                    continue
                partial.unlink(missing_ok=True)
                job.status = "skipped"
                job.error = None
                return

            os.replace(partial, job.destination)
            job.status = "completed"
            job.total_bytes = job.bytes_downloaded
            return


__all__ = ["DownloadJob", "DownloadManager", "create_manual_job", "parse_manifest"]
