import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app_paths import get_app_directory
from downloader import DownloadJob, DownloadManager, create_manual_job, parse_manifest
import main as downloader_app


class AppPathTests(unittest.TestCase):
    def test_source_application_directory_is_project_directory(self) -> None:
        self.assertEqual(get_app_directory(), Path(__file__).resolve().parents[1])


class AutoCloseTests(unittest.TestCase):
    def test_auto_close_requires_flag_and_empty_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            with patch.object(downloader_app, "DATA_DIR", data_dir):
                with patch.object(
                    downloader_app.sys, "argv", ["main.py"]
                ):
                    self.assertFalse(downloader_app._auto_close_if_data_empty())

                with patch.object(
                    downloader_app.sys, "argv", ["main.py", "--auto-close"]
                ):
                    with patch.object(downloader_app.app.native, "main_window") as window:
                        self.assertTrue(downloader_app._auto_close_if_data_empty())
                        window.destroy.assert_called_once_with()
                        (data_dir / "keep.txt").write_text("keep", encoding="utf-8")
                        self.assertFalse(downloader_app._auto_close_if_data_empty())


class ManifestTests(unittest.TestCase):
    def test_parses_url_mapping_and_item_array(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            digest = hashlib.sha1(b"content").hexdigest()
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "https://example.com/file": {
                            "path": "downloads/file",
                            "sha1": digest,
                        }
                    }
                ),
                encoding="utf-8",
            )

            job = parse_manifest(manifest)[0]
            self.assertEqual(job.url, "https://example.com/file")
            self.assertEqual(job.sha1, digest)
            self.assertEqual(job.destination, Path.cwd() / "downloads" / "file")

            manifest.write_text(
                json.dumps(
                    [
                        {
                            "url": "https://example.com/other",
                            "path": str(root / "other"),
                            "sha1": digest,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(parse_manifest(manifest)[0].url, "https://example.com/other")

    def test_manifest_sha1_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = Path(temporary_directory) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "https://example.com/file": {
                            "path": str(Path(temporary_directory) / "new" / "file")
                        }
                    }
                ),
                encoding="utf-8",
            )

            job = parse_manifest(manifest)[0]
            self.assertIsNone(job.sha1)
            self.assertEqual(job.destination.parent.name, "new")

    def test_rejects_invalid_sha1(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = Path(temporary_directory) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "https://example.com/file": {
                            "path": "file",
                            "sha1": "not-a-sha1",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "sha1"):
                parse_manifest(manifest)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def test_manifest_is_deleted_only_after_all_jobs_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = Path(temporary_directory) / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            jobs = [
                DownloadJob(
                    "https://example.com/one",
                    Path(temporary_directory) / "one",
                    None,
                    manifest,
                    status="completed",
                ),
                DownloadJob(
                    "https://example.com/two",
                    Path(temporary_directory) / "two",
                    None,
                    manifest,
                    status="downloading",
                ),
            ]
            previous_jobs = downloader_app.jobs
            previous_seen = downloader_app.seen_manifests.copy()
            previous_errors = downloader_app.manifest_errors.copy()
            try:
                downloader_app.jobs = jobs
                downloader_app.seen_manifests = {manifest}
                downloader_app.manifest_errors = {}
                downloader_app.cleanup_finished_manifests()
                self.assertTrue(manifest.exists())

                jobs[1].status = "skipped"
                downloader_app.cleanup_finished_manifests()
                self.assertFalse(manifest.exists())
                self.assertNotIn(manifest, downloader_app.seen_manifests)
            finally:
                downloader_app.jobs = previous_jobs
                downloader_app.seen_manifests = previous_seen
                downloader_app.manifest_errors = previous_errors

    async def test_manual_job_downloads_without_sha1(self) -> None:
        payload = b"manual download"

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload, request=_request)

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "new" / "folder" / "manual.bin"
            job = create_manual_job(" https://example.com/file ", str(destination))
            self.assertIsNone(job.sha1)
            self.assertEqual(job.destination, destination.resolve())

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await DownloadManager().download(job, client)

            self.assertEqual(job.status, "completed")
            self.assertEqual(job.bytes_downloaded, len(payload))
            self.assertEqual(destination.read_bytes(), payload)

    async def test_sha1_mismatch_retries_three_times_then_skips_silently(self) -> None:
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, content=b"wrong content", request=request)

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "file.bin"
            job = DownloadJob(
                "https://example.com/file",
                destination,
                hashlib.sha1(b"expected content").hexdigest(),
                Path("manifest.json"),
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await DownloadManager().download(job, client)

            self.assertEqual(attempts, 4)
            self.assertEqual(job.status, "skipped")
            self.assertIsNone(job.error)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name("file.bin.part").exists())

    def test_manual_job_requires_http_url_and_destination(self) -> None:
        with self.assertRaisesRegex(ValueError, "URL"):
            create_manual_job("", "downloads/file")
        with self.assertRaisesRegex(ValueError, "保存路径"):
            create_manual_job("https://example.com/file", " ")
        with self.assertRaisesRegex(ValueError, "不支持"):
            create_manual_job("file:///tmp/file", "downloads/file")

    async def test_resumes_partial_file_with_range_request(self) -> None:
        payload = b"resumable download"
        requested_ranges: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            range_header = request.headers.get("range")
            self.assertIsNotNone(range_header)
            requested_ranges.append(range_header)
            offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(
                206,
                headers={
                    "content-range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}",
                    "content-length": str(len(payload) - offset),
                },
                content=payload[offset:],
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "file.bin"
            partial = destination.with_name(destination.name + ".part")
            partial.write_bytes(payload[:5])
            job = DownloadJob(
                "https://example.com/file",
                destination,
                hashlib.sha1(payload).hexdigest(),
                Path("manifest.json"),
            )
            manager = DownloadManager()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await manager.download(job, client)

            self.assertEqual(job.status, "completed")
            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(partial.exists())
            self.assertEqual(requested_ranges, ["bytes=5-"])

    async def test_restarts_when_server_ignores_range_request(self) -> None:
        payload = b"complete response"
        requested_ranges: list[str | None] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requested_ranges.append(request.headers.get("range"))
            return httpx.Response(200, content=payload)

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "file.bin"
            partial = destination.with_name(destination.name + ".part")
            partial.write_bytes(b"old partial bytes")
            job = DownloadJob(
                "https://example.com/file",
                destination,
                hashlib.sha1(payload).hexdigest(),
                Path("manifest.json"),
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await DownloadManager().download(job, client)

            self.assertEqual(job.status, "completed")
            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(partial.exists())
            self.assertEqual(requested_ranges, ["bytes=17-"])

    async def test_retries_from_start_when_partial_sha1_does_not_match(self) -> None:
        payload = b"trusted response"
        requested_ranges: list[str | None] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            range_header = request.headers.get("range")
            requested_ranges.append(range_header)
            if range_header:
                offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
                return httpx.Response(
                    206,
                    headers={
                        "content-range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}"
                    },
                    content=payload[offset:],
                )
            return httpx.Response(200, content=payload)

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "file.bin"
            partial = destination.with_name(destination.name + ".part")
            partial.write_bytes(b"bad")
            job = DownloadJob(
                "https://example.com/file",
                destination,
                hashlib.sha1(payload).hexdigest(),
                Path("manifest.json"),
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await DownloadManager().download(job, client)

            self.assertEqual(job.status, "completed")
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(requested_ranges, ["bytes=3-", None])

    async def test_limits_connections_per_server_to_eight(self) -> None:
        active = 0
        peak_active = 0
        lock = asyncio.Lock()

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal active, peak_active
            async with lock:
                active += 1
                peak_active = max(peak_active, active)
            await asyncio.sleep(0.01)
            async with lock:
                active -= 1
            return httpx.Response(200, content=b"ok", request=_request)

        payload_sha1 = hashlib.sha1(b"ok").hexdigest()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = [
                DownloadJob(
                    f"https://same.example:{443 if index < 6 else 8443}/file-{index}",
                    root / f"file-{index}",
                    payload_sha1,
                    root / "manifest.json",
                )
                for index in range(12)
            ]
            manager = DownloadManager()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await asyncio.gather(*(manager.download(job, client) for job in jobs))

            self.assertLessEqual(peak_active, 8)
            self.assertGreater(peak_active, 1)
            self.assertTrue(all(job.status == "completed" for job in jobs))


if __name__ == "__main__":
    unittest.main()
