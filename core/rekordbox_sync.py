from __future__ import annotations

import hashlib
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
from PySide6 import QtCore

from core.analysis_lib_handler import FeatureNPZStore
from core.library_handler import TrackRow
from third_party.rekordbox import (
    RekordboxXmlDocument,
    build_rekordbox_track_element,
    rekordbox_track_key_for_row,
)
from utils.atomic_io import atomic_write_text


@dataclass(frozen=True)
class RekordboxSyncRequest:
    library_dir: Path
    xml_path: Path
    mode: str
    full_rebuild: bool = False
    rows: tuple[TrackRow, ...] = ()
    uid: str = ""
    show_progress: bool = False


@dataclass(frozen=True)
class RekordboxSyncResult:
    output_path: Path
    processed_count: int
    reused_count: int
    entry_count: int


class RekordboxSyncWorker(QtCore.QObject):
    progress = QtCore.Signal(int, str)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, request: RekordboxSyncRequest, parent=None) -> None:
        super().__init__(parent)
        self._request = request

    @QtCore.Slot()
    def run(self) -> None:
        try:
            engine = _RekordboxSyncEngine(self._request, self.progress.emit)
            self.finished.emit(engine.run())
        except Exception:
            self.failed.emit(traceback.format_exc())


class RekordboxXmlSync(QtCore.QObject):
    started = QtCore.Signal(object)
    progress = QtCore.Signal(object, int, str)
    finished = QtCore.Signal(object, object)
    failed = QtCore.Signal(object, str)

    def __init__(self, *, cfg_getter: Callable[[], object], parent=None) -> None:
        super().__init__(parent)
        self._cfg_getter = cfg_getter
        self._queue: list[RekordboxSyncRequest] = []
        self._active_request: RekordboxSyncRequest | None = None
        self._thread: QtCore.QThread | None = None
        self._worker: RekordboxSyncWorker | None = None

    @QtCore.Slot(object)
    def sync_incremental(self, rows: object) -> None:
        track_rows = self._coerce_rows(rows)
        if track_rows is None:
            return
        request = self._request_from_config(mode="incremental", rows=tuple(track_rows))
        if request is not None:
            self._enqueue(request)

    @QtCore.Slot(object)
    def sync_requested(self, payload: object) -> None:
        if isinstance(payload, dict):
            full_rebuild = bool(payload.get("full_rebuild", True))
            show_progress = bool(payload.get("show_progress", False))
        else:
            full_rebuild = bool(payload)
            show_progress = False
        request = self._request_from_config(
            mode="library",
            full_rebuild=full_rebuild,
            show_progress=show_progress,
        )
        if request is not None:
            self._enqueue(request)

    @QtCore.Slot(str)
    def sync_track_requested(self, uid: str) -> None:
        track_uid = str(uid or "").strip()
        if not track_uid:
            return
        request = self._request_from_config(mode="track", uid=track_uid)
        if request is not None:
            self._enqueue(request)

    def _request_from_config(self, *, mode: str, **kwargs) -> RekordboxSyncRequest | None:
        cfg = self._cfg_getter()
        libcfg = getattr(cfg, "libconfig", None) if cfg is not None else None
        if libcfg is None or not bool(getattr(libcfg, "rekordbox_sync_enabled", False)):
            return None
        xml_path_text = str(getattr(libcfg, "rekordbox_xml_path", "") or "").strip()
        if not xml_path_text:
            return None
        return RekordboxSyncRequest(
            library_dir=Path(str(libcfg.libpath)).expanduser(),
            xml_path=Path(xml_path_text).expanduser(),
            mode=mode,
            **kwargs,
        )

    def _enqueue(self, request: RekordboxSyncRequest) -> None:
        self._queue.append(request)
        if self._thread is None:
            self._start_next()

    def _start_next(self) -> None:
        if self._thread is not None or not self._queue:
            return
        request = self._queue.pop(0)
        thread = QtCore.QThread(self)
        worker = RekordboxSyncWorker(request)
        worker.moveToThread(thread)
        worker.progress.connect(self._relay_progress)
        worker.finished.connect(self._job_finished)
        worker.failed.connect(self._job_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        thread.started.connect(worker.run)
        self._active_request = request
        self._thread = thread
        self._worker = worker
        self.started.emit(request)
        thread.start()

    @QtCore.Slot(int, str)
    def _relay_progress(self, value: int, message: str) -> None:
        if self._active_request is not None:
            self.progress.emit(self._active_request, int(value), str(message))

    @QtCore.Slot(object)
    def _job_finished(self, result: RekordboxSyncResult) -> None:
        if self._active_request is not None:
            self.finished.emit(self._active_request, result)

    @QtCore.Slot(str)
    def _job_failed(self, message: str) -> None:
        if self._active_request is not None:
            self.failed.emit(self._active_request, str(message))

    @QtCore.Slot()
    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._active_request = None
        QtCore.QTimer.singleShot(0, self._start_next)

    @staticmethod
    def _coerce_rows(rows: object) -> list[TrackRow] | None:
        if rows is None:
            return []
        result: list[TrackRow] = []
        try:
            iterator: Iterable = iter(rows)
        except TypeError:
            return None
        for row in iterator:
            if isinstance(row, TrackRow):
                result.append(row)
            elif isinstance(row, dict):
                try:
                    result.append(TrackRow(**row))
                except Exception:
                    continue
        return result


class _RekordboxSyncEngine:
    def __init__(
        self,
        request: RekordboxSyncRequest,
        progress: Callable[[int, str], None],
    ) -> None:
        self.request = request
        self.progress = progress
        self.store = FeatureNPZStore(base_dir=str(request.library_dir), compressed=True)

    def run(self) -> RekordboxSyncResult:
        self.request.xml_path.parent.mkdir(parents=True, exist_ok=True)
        if self.request.mode == "library":
            self.progress(1, "Loading Mixlyzer library")
            rows = self._load_library_rows()
            return self._sync_library(rows)
        if self.request.mode == "incremental":
            return self._sync_incremental(list(self.request.rows))
        if self.request.mode == "track":
            return self._sync_track(self.request.uid)
        raise ValueError(f"Unsupported Rekordbox sync mode: {self.request.mode}")

    def _sync_library(self, rows: list[TrackRow]) -> RekordboxSyncResult:
        self.progress(4, "Loading Rekordbox XML")
        document = RekordboxXmlDocument.load(
            self.request.xml_path, force_new=self.request.full_rebuild
        )
        nodes = []
        reused = 0
        total = len(rows)
        for index, row in enumerate(rows, start=1):
            track_key = rekordbox_track_key_for_row(row)
            track_hash = self._track_hash(row)
            previous = document.existing_track(track_key)
            if (
                previous is not None
                and not self.request.full_rebuild
                and str(previous.attrib.get("MixlyzerHash", "")) == track_hash
            ):
                node = previous
                reused += 1
            else:
                node = self._build_track(row, track_hash)
            nodes.append(node)
            self.progress(
                5 + int(round(index * 85 / max(total, 1))),
                f"Syncing tracks {index}/{total}",
            )
        document.replace_tracks(nodes)
        self._write(document)
        return RekordboxSyncResult(
            self.request.xml_path, total, reused, len(nodes)
        )

    def _sync_incremental(self, rows: list[TrackRow]) -> RekordboxSyncResult:
        self.progress(4, "Loading Rekordbox XML")
        document = RekordboxXmlDocument.load(
            self.request.xml_path, force_new=False
        )
        total = len(rows)
        for index, row in enumerate(rows, start=1):
            document.remove_track(rekordbox_track_key_for_row(row))
            document.append_track(self._build_track(row, self._track_hash(row)))
            self.progress(
                5 + int(round(index * 85 / max(total, 1))),
                f"Syncing changed tracks {index}/{total}",
            )
        entry_count = document.entry_count
        self._write(document)
        return RekordboxSyncResult(self.request.xml_path, total, 0, entry_count)

    def _sync_track(self, uid: str) -> RekordboxSyncResult:
        self.progress(4, "Loading Rekordbox XML")
        document = RekordboxXmlDocument.load(
            self.request.xml_path, force_new=False
        )
        row = self._load_track_row(uid)
        document.remove_track(uid)
        processed = 0
        if row is not None:
            self.progress(45, "Building Rekordbox track entry")
            document.append_track(self._build_track(row, self._track_hash(row)))
            processed = 1
        entry_count = document.entry_count
        self._write(document)
        return RekordboxSyncResult(self.request.xml_path, processed, 0, entry_count)

    def _build_track(self, row: TrackRow, track_hash: str):
        return build_rekordbox_track_element(
            row,
            self._load_features(row.uid),
            resolve_path=self._resolve_audio_path,
            extra_attributes={"MixlyzerUID": str(row.uid or ""), "MixlyzerHash": track_hash},
        )

    def _write(self, document: RekordboxXmlDocument) -> None:
        self.progress(92, "Formatting Rekordbox XML")
        xml_text = document.to_xml()
        self.progress(97, "Writing Rekordbox XML")
        atomic_write_text(self.request.xml_path, xml_text)
        self.progress(100, "Rekordbox XML sync complete")

    def _load_features(self, uid: Optional[str]) -> dict[str, np.ndarray]:
        if not uid:
            return {}
        try:
            return self.store.load(uid)
        except Exception:
            return {}

    def _track_hash(self, row: TrackRow) -> str:
        parts = [str(v) for k, v in sorted(asdict(row).items())]
        uid = str(row.uid or "").strip()
        npz_path = Path(self.store.path(uid)) if uid else None
        if npz_path is not None and npz_path.exists():
            stat = npz_path.stat()
            parts.append(str(stat.st_mtime_ns))
            parts.append(str(stat.st_size))
        digest = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()
        return digest

    def _load_library_rows(self) -> list[TrackRow]:
        from core.library_handler import LibraryDB

        db = LibraryDB(str(self.request.library_dir / "library.db"))
        db.connect()
        try:
            return db.list_all()
        finally:
            db.close()

    def _load_track_row(self, uid: str) -> TrackRow | None:
        from core.library_handler import LibraryDB

        db = LibraryDB(str(self.request.library_dir / "library.db"))
        db.connect()
        try:
            return db.get_by_uid(uid)
        finally:
            db.close()

    @staticmethod
    def _resolve_audio_path(path: str | None) -> Optional[Path]:
        raw = str(path or "").strip()
        if not raw:
            return None
        # Intentionally returns the candidate even when it does not exist:
        # callers re-check .exists() and fall back to the stored path/size.
        return Path(raw).expanduser()
