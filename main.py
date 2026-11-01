import asyncio
from pathlib import Path

import httpx
from nicegui import app, ui

from downloader import DownloadJob, DownloadManager, create_manual_job, parse_manifest


DATA_DIR = Path(__file__).parent / "data"
jobs: list[DownloadJob] = []
manifest_errors: dict[Path, str] = {}
seen_manifests: set[Path] = set()
seen_destinations: set[Path] = set()
manager = DownloadManager()
download_client: httpx.AsyncClient | None = None
monitor_task: asyncio.Task[None] | None = None

STATUS_LABELS = {
    "queued": "等待中",
    "checking": "检查文件",
    "downloading": "下载中",
    "completed": "已完成",
    "failed": "下载失败",
    "skipped": "已跳过",
}


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _register_jobs(new_jobs: list[DownloadJob], client: httpx.AsyncClient) -> None:
    destinations = [job.destination for job in new_jobs]
    duplicates = set(destinations) & seen_destinations
    if duplicates:
        names = ", ".join(str(path) for path in sorted(duplicates))
        raise ValueError(f"保存路径已在下载队列中: {names}")

    seen_destinations.update(destinations)
    jobs.extend(new_jobs)
    for job in new_jobs:
        asyncio.create_task(manager.download(job, client))


@ui.refreshable
def render_stats() -> None:
    counts = {
        "全部任务": len(jobs),
        "进行中": sum(job.status in {"queued", "checking", "downloading"} for job in jobs),
        "已完成": sum(job.status == "completed" for job in jobs),
        "已跳过": sum(job.status == "skipped" for job in jobs),
        "失败": sum(job.status == "failed" for job in jobs),
    }
    with ui.grid(columns=5).classes("w-full gap-3"):
        for label, count in counts.items():
            with ui.card().classes("border border-slate-800 bg-slate-900/70 p-4"):
                ui.label(label).classes("text-xs text-slate-400")
                ui.label(str(count)).classes("mt-1 text-2xl font-bold text-white")


@ui.refreshable
def render_jobs() -> None:
    if manifest_errors:
        with ui.column().classes("w-full gap-2"):
            for error in manifest_errors.values():
                with ui.row().classes(
                    "w-full items-center gap-2 rounded-xl bg-red-950/60 p-3 text-red-200"
                ):
                    ui.icon("warning").classes("text-red-400")
                    ui.label(error).classes("text-sm")

    if not jobs:
        with ui.card().classes(
            "w-full items-center border border-dashed border-slate-700 bg-slate-900/50 py-12"
        ):
            ui.icon("cloud_download").classes("text-4xl text-slate-500")
            ui.label("还没有下载任务").classes("mt-3 text-lg font-semibold text-slate-200")
            ui.label("粘贴下载链接，或将 JSON 清单放进 data 文件夹。").classes(
                "text-sm text-slate-400"
            )
        return

    with ui.column().classes("w-full gap-3"):
        for job in reversed(jobs):
            is_active = job.status in {"queued", "checking", "downloading"}
            status_color = {
                "completed": "text-emerald-300 bg-emerald-400/10",
                "failed": "text-red-300 bg-red-400/10",
                "skipped": "text-slate-300 bg-slate-400/10",
                "downloading": "text-blue-300 bg-blue-400/10",
                "checking": "text-amber-300 bg-amber-400/10",
                "queued": "text-slate-300 bg-slate-400/10",
            }.get(job.status, "text-slate-300 bg-slate-400/10")

            with ui.card().classes(
                "w-full border border-slate-800 bg-slate-900/80 p-4 shadow-lg shadow-black/10"
            ):
                with ui.row().classes("w-full items-start justify-between gap-3"):
                    with ui.row().classes("min-w-0 flex-1 items-center gap-3"):
                        icon, icon_color = (
                            ("check_circle", "text-emerald-400")
                            if job.status == "completed"
                            else ("remove_circle", "text-slate-400")
                            if job.status == "skipped"
                            else ("error", "text-red-400")
                            if job.status == "failed"
                            else ("downloading", "text-blue-400")
                            if job.status == "downloading"
                            else ("schedule", "text-slate-400")
                        )
                        ui.icon(icon).classes(f"text-2xl {icon_color}")
                        with ui.column().classes("min-w-0 gap-0"):
                            ui.label(job.destination.name).classes(
                                "max-w-full truncate font-semibold text-slate-100"
                            )
                            ui.label(job.url).classes(
                                "max-w-full truncate text-xs text-slate-400"
                            )
                    ui.label(STATUS_LABELS.get(job.status, job.status)).classes(
                        f"shrink-0 rounded-full px-3 py-1 text-xs font-medium {status_color}"
                    )

                if job.status == "downloading":
                    if job.total_bytes:
                        progress = min(job.bytes_downloaded / job.total_bytes, 1)
                        ui.linear_progress(value=progress, show_value=False).props(
                            "rounded"
                        ).classes("mt-4 h-2")
                        progress_text = (
                            f"{progress:.1%} · {_format_size(job.bytes_downloaded)}"
                            f" / {_format_size(job.total_bytes)}"
                        )
                    else:
                        progress_text = f"已接收 {_format_size(job.bytes_downloaded)}"
                    ui.label(progress_text).classes("mt-2 text-xs text-slate-400")
                elif job.status == "completed":
                    ui.linear_progress(value=1, show_value=False).props("rounded").classes(
                        "mt-4 h-2"
                    )
                    ui.label(f"完成 · {_format_size(job.bytes_downloaded)}").classes(
                        "mt-2 text-xs text-emerald-300"
                    )
                elif job.status == "failed" and job.error:
                    ui.label(job.error).classes("mt-3 whitespace-normal text-sm text-red-300")
                elif is_active:
                    ui.label("等待可用连接…" if job.status == "queued" else "正在准备…").classes(
                        "mt-2 text-xs text-slate-400"
                    )


async def monitor_manifests(client: httpx.AsyncClient) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        for manifest in sorted(DATA_DIR.glob("*.json")):
            resolved_manifest = manifest.resolve()
            if resolved_manifest in seen_manifests:
                continue
            try:
                manifest_jobs = parse_manifest(resolved_manifest)
                _register_jobs(manifest_jobs, client)
                seen_manifests.add(resolved_manifest)
                manifest_errors.pop(resolved_manifest, None)
            except ValueError as error:
                manifest_errors[resolved_manifest] = f"{manifest.name}: {error}"
            except OSError as error:
                manifest_errors[resolved_manifest] = f"无法处理 {manifest.name}: {error}"
        render_stats.refresh()
        render_jobs.refresh()
        await asyncio.sleep(1)


async def start_services() -> None:
    global download_client, monitor_task
    timeout = httpx.Timeout(connect=20, read=None, write=20, pool=20)
    download_client = httpx.AsyncClient(follow_redirects=True, timeout=timeout)
    monitor_task = asyncio.create_task(monitor_manifests(download_client))


async def stop_services() -> None:
    if monitor_task is not None:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
    if download_client is not None:
        await download_client.aclose()


def add_manual_download(url_input: ui.input, path_input: ui.input) -> None:
    if download_client is None:
        ui.notify("下载服务尚未启动，请稍后重试。", type="warning")
        return
    try:
        job = create_manual_job(url_input.value or "", path_input.value or "")
        _register_jobs([job], download_client)
    except ValueError as error:
        ui.notify(str(error), type="negative")
        return

    ui.notify("已添加到下载队列", type="positive")
    url_input.value = ""
    render_stats.refresh()
    render_jobs.refresh()


@ui.page("/")
def index() -> None:
    ui.query("body").classes("bg-slate-950 text-slate-100")
    with ui.column().classes("mx-auto w-full max-w-5xl gap-6 px-4 py-8 md:px-8 md:py-12"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.row().classes("items-center gap-3"):
                with ui.element("div").classes(
                    "rounded-2xl bg-blue-500/15 p-3 ring-1 ring-blue-400/20"
                ):
                    ui.icon("downloading").classes("text-3xl text-blue-300")
                with ui.column().classes("gap-0"):
                    ui.label("MWP Downloader").classes(
                        "text-2xl font-bold tracking-tight text-white md:text-3xl"
                    )
                    ui.label("让下载井然有序").classes("text-sm text-slate-400")
            with ui.row().classes(
                "hidden items-center gap-2 rounded-full border border-slate-800 bg-slate-900 px-3 py-2 sm:flex"
            ):
                ui.icon("folder_open").classes("text-sm text-blue-300")
                ui.label("自动监视 data 文件夹").classes("text-xs text-slate-300")

        with ui.card().classes(
            "w-full border border-slate-800 bg-gradient-to-br from-slate-900 to-slate-900/70 p-5 shadow-xl shadow-black/20 md:p-7"
        ):
            with ui.row().classes("mb-5 items-center gap-3"):
                with ui.element("div").classes("rounded-xl bg-blue-500/10 p-2"):
                    ui.icon("add_link").classes("text-xl text-blue-300")
                with ui.column().classes("gap-0"):
                    ui.label("添加下载").classes("text-lg font-semibold text-white")
                    ui.label("无需 SHA1，输入链接和保存位置即可开始。").classes(
                        "text-sm text-slate-400"
                    )

            with ui.row().classes("w-full items-end gap-3"):
                with ui.column().classes("min-w-0 flex-1 gap-3"):
                    url_input = ui.input(
                        "下载链接",
                        placeholder="https://example.com/archive.zip",
                    ).props("outlined dark rounded").classes("w-full")
                    path_input = ui.input(
                        "保存路径",
                        placeholder="downloads/archive.zip",
                    ).props("outlined dark rounded").classes("w-full")
                ui.button(
                    "开始下载",
                    icon="download",
                    on_click=lambda: add_manual_download(url_input, path_input),
                ).props("color=primary unelevated").classes(
                    "h-12 shrink-0 rounded-xl px-5 font-semibold"
                )

            ui.label(
                "相对路径保存到项目目录；已有同名任务时会提示冲突。"
            ).classes("mt-3 text-xs text-slate-500")

        render_stats()

        with ui.row().classes("w-full items-center justify-between"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("format_list_bulleted").classes("text-blue-300")
                ui.label("下载队列").classes("text-lg font-semibold text-white")
            ui.label(f"清单目录：{DATA_DIR}").classes(
                "hidden max-w-[60%] truncate text-xs text-slate-500 md:block"
            )
        render_jobs()
        ui.timer(1, lambda: (render_stats.refresh(), render_jobs.refresh()))


def main() -> None:
    app.on_startup(start_services)
    app.on_shutdown(stop_services)
    ui.run(title="MWP Downloader", reload=False, native=True)


if __name__ == "__main__":
    main()
