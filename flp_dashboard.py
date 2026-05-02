#!/usr/bin/env python3
"""Dash dashboard for viewing .flp project file metadata within a folder."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sqlite3
import threading
import traceback
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

import dash
from dash import Input, Output, State, callback_context, dash_table, dcc, html, no_update
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from flask import jsonify, request

from flp_metadata import build_metadata, parse_flp


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "flp_dashboard.sqlite3"
UPLOAD_DIR = APP_DIR / ".flp_dashboard_uploads"
DEFAULT_SCAN_PATH = r"%USERPROFILE%\Documents\Image-Line\FL Studio\Projects"

PAGE_SIZE = 18
BASE_PROJECT_TABLE_STYLES = [
    {"if": {"row_index": "odd"}, "backgroundColor": "#151b24"},
    {"if": {"state": "active"}, "backgroundColor": "#263442", "border": "0"},
]

JOB_LOCK = threading.Lock()
CURRENT_JOB: dict[str, Any] = {
    "active": False,
    "kind": "",
    "label": "",
    "total": 0,
    "done": 0,
    "ok": 0,
    "errors": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
    "message": "Idle",
}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def local_now_label() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                folder TEXT NOT NULL,
                size INTEGER NOT NULL,
                modified TEXT,
                created_fs TEXT,
                sha256 TEXT NOT NULL,
                group_key TEXT NOT NULL,
                group_name TEXT NOT NULL,
                duplicate_reason TEXT,
                parsed_at TEXT NOT NULL,
                parse_error TEXT,
                fl_version TEXT,
                fl_build INTEGER,
                title TEXT,
                comments TEXT,
                genre TEXT,
                artists TEXT,
                created_on TEXT,
                time_spent_seconds REAL,
                tempo_bpm REAL,
                channel_count INTEGER,
                ppq INTEGER,
                plugin_count INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                plugin_path_count INTEGER NOT NULL DEFAULT 0,
                text_event_count INTEGER NOT NULL DEFAULT 0,
                event_count INTEGER NOT NULL DEFAULT 0,
                unique_event_ids INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_projects_group_key ON projects(group_key);
            CREATE INDEX IF NOT EXISTS idx_projects_sha256 ON projects(sha256);
            CREATE INDEX IF NOT EXISTS idx_projects_modified ON projects(modified);

            CREATE TABLE IF NOT EXISTS scan_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                message TEXT NOT NULL,
                traceback TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_plugins (
                project_id INTEGER NOT NULL,
                plugin_name TEXT NOT NULL,
                plugin_path TEXT,
                vendor TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_project_plugins_project_id ON project_plugins(project_id);
            CREATE INDEX IF NOT EXISTS idx_project_plugins_name ON project_plugins(plugin_name);

            CREATE TABLE IF NOT EXISTS project_samples (
                project_id INTEGER NOT NULL,
                sample_path TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_project_samples_project_id ON project_samples(project_id);
            CREATE INDEX IF NOT EXISTS idx_project_samples_path ON project_samples(sample_path);
            """
        )


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def seconds_to_label(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    whole = int(round(float(seconds)))
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def compact_datetime(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return text.split(".")[0]
    return parsed.strftime("%Y-%m-%d %H:%M")


def compact_count(value: int | float | None) -> str:
    if value is None:
        return "0"
    number = float(value)
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if abs(number) >= 10_000:
        return f"{number / 1_000:.1f}K"
    if float(number).is_integer():
        return f"{int(number):,}"
    return f"{number:,.1f}"


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fl_major_version(version: Any) -> int | None:
    match = re.match(r"\s*(\d+)", str(version or ""))
    return int(match.group(1)) if match else None


def safe_tempo(value: Any, fl_version: Any = None) -> float | None:
    tempo = safe_float(value)
    major = fl_major_version(fl_version)
    max_tempo = 522.0 if major is not None and major >= 11 else 999.0
    if tempo is None or tempo < 10 or tempo > max_tempo:
        return None
    return tempo


def format_tempo(value: Any, fl_version: Any = None) -> str:
    tempo = safe_tempo(value, fl_version)
    if tempo is None:
        return ""
    if tempo.is_integer():
        return str(int(tempo))
    return f"{tempo:.3f}".rstrip("0").rstrip(".")


def safe_time_spent_seconds(value: Any) -> float | None:
    seconds = safe_float(value)
    if seconds is None or seconds < 0 or seconds > 3650 * 24 * 60 * 60:
        return None
    return seconds


def table_filter_string(value: Any) -> str:
    return json.dumps(str(value or ""))


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def normalize_for_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def is_reasonable_label(value: str) -> bool:
    text = value.strip()
    if len(text) < 2:
        return False
    if "\x00" in text:
        return False
    printable = [char for char in text if char.isprintable() or char in "\t\r\n"]
    if len(printable) / max(1, len(text)) < 0.95:
        return False
    return any(char.isalnum() for char in text)


def clean_project_family_name(path: Path, metadata: dict[str, Any]) -> tuple[str, str]:
    title = str(metadata.get("project", {}).get("title") or "").strip()
    use_title = is_reasonable_label(title)
    raw_name = title if use_title else path.stem
    reason = "project title" if use_title else "filename cleanup"
    name = raw_name.replace("_", " ").replace(".", " ")
    name = re.sub(r"\s+", " ", name).strip()
    backup_context = any(
        token in normalize_for_key(part)
        for part in path.parts
        for token in ("backup", "autosave", "auto save", "recovery", "recovered")
    )

    fl_backup_patterns = [
        r"\s*\((?:overwritten|autosaved|auto saved)\s+at\s+[^)]*\)\s*(?:[\s\-_]*\d+)?$",
        r"[\s\-_]+(?:overwritten|autosaved|auto saved)\s+at\s+[a-z0-9:._ -]+(?:[\s\-_]*\d+)?$",
        r"\s*\((?:backup|bak|autosave|auto save|recovered|recovery|copy)(?:[^)]*)\)\s*(?:[\s\-_]*\d+)?$",
    ]
    had_backup_suffix = False
    previous = None
    while previous != name:
        previous = name
        for pattern in fl_backup_patterns:
            updated = re.sub(pattern, "", name, flags=re.IGNORECASE).strip()
            if updated != name:
                had_backup_suffix = True
                name = updated

    cleanup_patterns = [
        r"[\s\-_()]+(?:20\d{2}[-_. ]?\d{2}[-_. ]?\d{2})(?:[-_. ]?\d{1,2}[-_. ]?\d{2}(?:[-_. ]?\d{2})?)?$",
        r"[\s\-_()]+(?:\d{8})(?:[-_. ]?\d{1,2}[-_. ]?\d{2}(?:[-_. ]?\d{2})?)?$",
        r"[\s\-_()]+(?:\d{1,2}[-_.]\d{1,2}[-_.]\d{2,4})(?:[-_. ]?\d{1,2}[-_.]\d{2})?$",
        r"[\s\-_()]+(?:v(?:er(?:sion)?)?|rev(?:ision)?|r)[\s\-_]*\d+[a-z]?$",
        r"[\s\-_()]+(?:backup|bak|copy|autosave|auto save|recovered|recovery)[\s\-_]*\d*$",
        r"[\s\-_()]+(?:old|new|final|final mix|finalmix|master|mixdown)[\s\-_]*\d*$",
        r"[\s\-_()]+(?:\(\d+\)|copy\s*\d+)$",
    ]
    previous = None
    while previous != name:
        previous = name
        for pattern in cleanup_patterns:
            name = re.sub(pattern, "", name, flags=re.IGNORECASE).strip()
        if backup_context or had_backup_suffix:
            name = re.sub(r"[\s\-_()]+(?:copy[\s\-_]*)?\d{1,4}$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+", " ", name).strip(" -_()[]")

    if not name:
        name = raw_name.strip() or path.stem
    return name, reason


def make_group_key(path: Path, metadata: dict[str, Any]) -> tuple[str, str, str]:
    group_name, reason = clean_project_family_name(path, metadata)
    normalized = normalize_for_key(group_name)
    if not normalized:
        normalized = normalize_for_key(path.stem) or metadata["file"]["sha256"][:16]
    return normalized, group_name, reason


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def metadata_plugin_name(plugin: dict[str, Any]) -> str:
    return str(plugin.get("name") or plugin.get("display_name") or plugin.get("internal_name") or "").strip()


def sync_project_side_tables(conn: sqlite3.Connection, project_id: int, metadata: dict[str, Any]) -> None:
    conn.execute("DELETE FROM project_plugins WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM project_samples WHERE project_id = ?", (project_id,))

    plugin_rows = []
    for plugin in metadata.get("plugins", []):
        name = metadata_plugin_name(plugin)
        if not name:
            continue
        plugin_rows.append((project_id, name, plugin.get("plugin_path") or "", plugin.get("vendor") or ""))
    if plugin_rows:
        conn.executemany(
            "INSERT INTO project_plugins(project_id, plugin_name, plugin_path, vendor) VALUES (?, ?, ?, ?)",
            plugin_rows,
        )

    sample_rows = [
        (project_id, sample)
        for sample in metadata.get("paths", {}).get("sample_paths", [])
        if isinstance(sample, str) and sample
    ]
    if sample_rows:
        conn.executemany(
            "INSERT INTO project_samples(project_id, sample_path) VALUES (?, ?)",
            sample_rows,
        )


def upsert_project(path: Path, metadata: dict[str, Any], parse_error: str | None = None) -> None:
    file_info = metadata["file"]
    header = metadata.get("header", {})
    project = metadata.get("project", {})
    paths = metadata.get("paths", {})
    group_key, group_name, reason = make_group_key(path, metadata)
    modified = file_info.get("modified")
    created_fs = file_info.get("created")

    row = {
        "path": str(path),
        "file_name": path.name,
        "folder": str(path.parent),
        "size": int(file_info.get("size") or path.stat().st_size),
        "modified": modified,
        "created_fs": created_fs,
        "sha256": file_info.get("sha256", ""),
        "group_key": group_key,
        "group_name": group_name,
        "duplicate_reason": reason,
        "parsed_at": utc_now(),
        "parse_error": parse_error,
        "fl_version": project.get("fl_version"),
        "fl_build": project.get("fl_build"),
        "title": project.get("title"),
        "comments": project.get("comments"),
        "genre": project.get("genre"),
        "artists": project.get("artists"),
        "created_on": project.get("created_on"),
        "time_spent_seconds": safe_time_spent_seconds(project.get("time_spent_seconds")),
        "tempo_bpm": safe_tempo(project.get("tempo_bpm"), project.get("fl_version")),
        "channel_count": header.get("channel_count"),
        "ppq": header.get("ppq"),
        "plugin_count": len(metadata.get("plugins", [])),
        "sample_count": len(paths.get("sample_paths", [])),
        "plugin_path_count": len(paths.get("plugin_paths", [])),
        "text_event_count": len(metadata.get("text_events", [])),
        "event_count": int(metadata.get("data_chunk", {}).get("event_count") or 0),
        "unique_event_ids": int(metadata.get("event_summary", {}).get("unique_event_ids") or 0),
        "metadata_json": json_dumps(metadata),
    }

    columns = list(row)
    placeholders = ", ".join(f":{column}" for column in columns)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "path")
    with get_conn() as conn:
        if parse_error is None:
            conn.execute("DELETE FROM scan_errors WHERE path = ?", (str(path),))
        conn.execute(
            f"""
            INSERT INTO projects ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(path) DO UPDATE SET {updates}
            """,
            row,
        )
        project_id = conn.execute("SELECT id FROM projects WHERE path = ?", (str(path),)).fetchone()["id"]
        sync_project_side_tables(conn, int(project_id), metadata)


def record_error(path: Path, message: str, tb: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM scan_errors WHERE path = ?", (str(path),))
        conn.execute(
            """
            INSERT INTO scan_errors(path, message, traceback, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (str(path), message, tb, utc_now()),
        )


def prune_scan_errors() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            DELETE FROM scan_errors
            WHERE EXISTS (
                SELECT 1
                FROM projects
                WHERE projects.path = scan_errors.path
                  AND projects.parse_error IS NULL
            )
            """
        )
        conn.execute(
            """
            DELETE FROM scan_errors
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM scan_errors
                GROUP BY path
            )
            """
        )


def parse_and_store(path: Path) -> None:
    base, events = parse_flp(path, text_limit=1200)
    metadata = build_metadata(
        base,
        events,
        text_limit=1200,
        include_events=False,
        include_embedded_strings=False,
    )
    upsert_project(path, metadata)


def update_job(**patch: Any) -> None:
    with JOB_LOCK:
        CURRENT_JOB.update(patch)


def job_snapshot() -> dict[str, Any]:
    with JOB_LOCK:
        return dict(CURRENT_JOB)


def iter_flp_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*.flp") if path.is_file())


def scan_worker(paths: list[Path], label: str, kind: str) -> None:
    update_job(
        active=True,
        kind=kind,
        label=label,
        total=len(paths),
        done=0,
        ok=0,
        errors=0,
        current="",
        started_at=utc_now(),
        finished_at=None,
        message=f"Scanning {len(paths)} FLP file(s)",
    )
    for index, path in enumerate(paths, start=1):
        update_job(done=index - 1, current=str(path), message=f"Parsing {path.name}")
        try:
            parse_and_store(path)
        except Exception as exc:  # noqa: BLE001 - scan should continue per file.
            record_error(path, str(exc), traceback.format_exc())
            update_job(errors=job_snapshot()["errors"] + 1)
        else:
            update_job(ok=job_snapshot()["ok"] + 1)
    update_job(
        active=False,
        done=len(paths),
        current="",
        finished_at=utc_now(),
        message=f"Finished at {local_now_label()}",
    )


def start_scan(paths: list[Path], label: str, kind: str) -> tuple[bool, str]:
    with JOB_LOCK:
        if CURRENT_JOB.get("active"):
            return False, "A scan is already running."
    thread = threading.Thread(target=scan_worker, args=(paths, label, kind), daemon=True)
    thread.start()
    return True, f"Started scanning {len(paths)} FLP file(s)."


def clear_dashboard_cache() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM projects")
        conn.execute("DELETE FROM scan_errors")
    if UPLOAD_DIR.exists():
        shutil.rmtree(UPLOAD_DIR)


def repair_cached_group_names() -> None:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, path, group_key, group_name, duplicate_reason, metadata_json FROM projects").fetchall()
        updates = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
                group_key, group_name, reason = make_group_key(Path(row["path"]), metadata)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                group_key != row["group_key"]
                or group_name != row["group_name"]
                or reason != row["duplicate_reason"]
            ):
                updates.append((group_key, group_name, reason, row["id"]))
        if updates:
            conn.executemany(
                "UPDATE projects SET group_key = ?, group_name = ?, duplicate_reason = ? WHERE id = ?",
                updates,
            )


def repair_cached_project_metrics() -> None:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, metadata_json
            FROM projects
            WHERE parse_error IS NULL
            """
        ).fetchall()
        updates = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            project = metadata.get("project", {})
            header = metadata.get("header", {})
            fl_version = project.get("fl_version")
            updates.append(
                (
                    fl_version,
                    project.get("fl_build"),
                    project.get("created_on"),
                    safe_time_spent_seconds(project.get("time_spent_seconds")),
                    safe_tempo(project.get("tempo_bpm"), fl_version),
                    header.get("channel_count"),
                    header.get("ppq"),
                    row["id"],
                )
            )
        if updates:
            conn.executemany(
                """
                UPDATE projects
                SET fl_version = ?,
                    fl_build = ?,
                    created_on = ?,
                    time_spent_seconds = ?,
                    tempo_bpm = ?,
                    channel_count = ?,
                    ppq = ?
                WHERE id = ?
                """,
                updates,
            )


def rebuild_side_tables_if_needed() -> None:
    with get_conn() as conn:
        project_count = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
        plugin_count = conn.execute("SELECT COUNT(*) AS c FROM project_plugins").fetchone()["c"]
        sample_count = conn.execute("SELECT COUNT(*) AS c FROM project_samples").fetchone()["c"]
        if not project_count or (plugin_count and sample_count):
            return
        rows = conn.execute("SELECT id, metadata_json FROM projects WHERE parse_error IS NULL").fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            sync_project_side_tables(conn, int(row["id"]), metadata)


def safe_upload_relative_path(name: str) -> Path:
    normalized = name.replace("\\", "/")
    parts = [
        part
        for part in PurePosixPath(normalized).parts
        if part not in ("", ".", "..") and not part.endswith(":")
    ]
    if not parts:
        parts = ["uploaded.flp"]
    return Path(*parts)


def register_upload_route(server) -> None:
    @server.post("/api/upload-folder")
    def upload_folder():
        files = request.files.getlist("files")
        if not files:
            return jsonify({"ok": False, "message": "No files received."}), 400

        batch_dir = UPLOAD_DIR / dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        batch_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for uploaded in files:
            relative = safe_upload_relative_path(uploaded.filename)
            if relative.suffix.lower() != ".flp":
                continue
            destination = batch_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            uploaded.save(destination)
            saved.append(destination)

        if not saved:
            return jsonify({"ok": False, "message": "No .flp files were included."}), 400

        started, message = start_scan(saved, f"Uploaded folder batch {batch_dir.name}", "upload")
        status = 202 if started else 409
        return jsonify({"ok": started, "message": message, "files": len(saved)}), status


def representative_cte(where_sql: str = "") -> str:
    return f"""
        WITH filtered AS (
            SELECT *
            FROM projects
            WHERE parse_error IS NULL
            {where_sql}
        ),
        group_stats AS (
            SELECT
                group_key,
                COUNT(*) AS file_count,
                COUNT(DISTINCT sha256) AS distinct_hash_count
            FROM filtered
            GROUP BY group_key
        ),
        ranked AS (
            SELECT
                filtered.*,
                group_stats.file_count,
                group_stats.distinct_hash_count,
                ROW_NUMBER() OVER (
                    PARTITION BY filtered.group_key
                    ORDER BY datetime(COALESCE(modified, '1970-01-01')) DESC, size DESC
                ) AS rn
            FROM filtered
            JOIN group_stats ON group_stats.group_key = filtered.group_key
        )
    """


def filter_clause(search: str | None, plugin: str | None) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    if search:
        like = f"%{search.strip()}%"
        clauses.append(
            """
            AND (
                group_name LIKE ?
                OR file_name LIKE ?
                OR folder LIKE ?
                OR title LIKE ?
                OR comments LIKE ?
                OR metadata_json LIKE ?
            )
            """
        )
        params.extend([like, like, like, like, like, like])
    if plugin:
        clauses.append("AND metadata_json LIKE ?")
        params.append(f"%{plugin}%")
    return "\n".join(clauses), params


def count_groups(search: str | None, plugin: str | None) -> int:
    where_sql, params = filter_clause(search, plugin)
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT group_key) AS count
            FROM projects
            WHERE parse_error IS NULL
            {where_sql}
            """,
            params,
        ).fetchone()
    return int(row["count"] or 0)


def query_group_rows(
    page_current: int,
    page_size: int,
    sort_by: list[dict[str, str]] | None,
    search: str | None,
    plugin: str | None,
) -> tuple[list[dict[str, Any]], int]:
    total = count_groups(search, plugin)
    where_sql, params = filter_clause(search, plugin)
    sort_map = {
        "project": "group_name COLLATE NOCASE",
        "latest_file": "file_name COLLATE NOCASE",
        "latest_modified": "datetime(COALESCE(modified, '1970-01-01'))",
        "files": "COALESCE(file_count, 0)",
        "variants": "COALESCE(distinct_hash_count, 0)",
        "samples": "COALESCE(sample_count, 0)",
        "plugins": "COALESCE(plugin_count, 0)",
        "fl_version": "COALESCE(fl_version, '') COLLATE NOCASE",
        "channels": "COALESCE(channel_count, -1)",
        "ppq": "COALESCE(ppq, -1)",
        "tempo": "COALESCE(tempo_bpm, -1)",
        "time_spent": "COALESCE(time_spent_seconds, -1)",
        "size": "COALESCE(size, 0)",
    }
    order_sql = "datetime(COALESCE(modified, '1970-01-01')) DESC, group_name COLLATE NOCASE ASC"
    if sort_by:
        sort = sort_by[0]
        column = sort_map.get(sort.get("column_id", ""), "group_name")
        direction = "DESC" if sort.get("direction") == "desc" else "ASC"
        order_sql = f"{column} {direction}, group_name COLLATE NOCASE ASC"

    offset = max(0, page_current or 0) * page_size
    with get_conn() as conn:
        rows = conn.execute(
            representative_cte(where_sql)
            + f"""
            SELECT *
            FROM ranked
            WHERE rn = 1
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        ).fetchall()

    data = []
    for row in rows:
        data.append(
            {
                "group_key": row["group_key"],
                "project": row["group_name"],
                "latest_file": row["file_name"],
                "latest_modified": compact_datetime(row["modified"]),
                "files": row["file_count"],
                "variants": row["distinct_hash_count"],
                "fl_version": row["fl_version"] or "",
                "channels": row["channel_count"],
                "ppq": row["ppq"],
                "tempo": format_tempo(row["tempo_bpm"], row["fl_version"]),
                "samples": row["sample_count"],
                "plugins": row["plugin_count"],
                "time_spent": seconds_to_label(row["time_spent_seconds"]),
                "size": human_bytes(row["size"]),
            }
        )
    pages = max(1, (total + page_size - 1) // page_size)
    return data, pages


def latest_group_rows() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            representative_cte()
            + """
            SELECT *
            FROM ranked
            WHERE rn = 1
            ORDER BY datetime(COALESCE(modified, '1970-01-01')) DESC
            """
        ).fetchall()


def load_metadata_json(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["metadata_json"])


def stats_snapshot() -> dict[str, Any]:
    with get_conn() as conn:
        total_files = conn.execute("SELECT COUNT(*) AS c FROM projects WHERE parse_error IS NULL").fetchone()["c"]
        physical_size = conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS c FROM projects WHERE parse_error IS NULL"
        ).fetchone()["c"]
        total_groups = conn.execute(
            "SELECT COUNT(DISTINCT group_key) AS c FROM projects WHERE parse_error IS NULL"
        ).fetchone()["c"]
        duplicate_groups = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM (
                SELECT group_key
                FROM projects
                WHERE parse_error IS NULL
                GROUP BY group_key
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()["c"]
        exact_duplicate_files = conn.execute(
            """
            SELECT COALESCE(SUM(file_count - distinct_hash_count), 0) AS c
            FROM (
                SELECT group_key, COUNT(*) AS file_count, COUNT(DISTINCT sha256) AS distinct_hash_count
                FROM projects
                WHERE parse_error IS NULL
                GROUP BY group_key
            )
            """
        ).fetchone()["c"]
        errors = conn.execute("SELECT COUNT(*) AS c FROM scan_errors").fetchone()["c"]
        rep_metrics = conn.execute(
            representative_cte()
            + """
            SELECT
                COALESCE(SUM(sample_count), 0) AS sample_refs,
                COALESCE(SUM(plugin_count), 0) AS plugin_instances,
                COALESCE(SUM(size), 0) AS canonical_size,
                COALESCE(SUM(time_spent_seconds), 0) AS canonical_time_seconds,
                COUNT(DISTINCT fl_version) AS versions
            FROM ranked
            WHERE rn = 1
            """
        ).fetchone()
        unique_samples = conn.execute(
            representative_cte()
            + """
            SELECT COUNT(DISTINCT project_samples.sample_path) AS c
            FROM ranked
            JOIN project_samples ON project_samples.project_id = ranked.id
            WHERE rn = 1
            """
        ).fetchone()["c"]
        plugin_stats = conn.execute(
            representative_cte()
            + """
            SELECT
                COUNT(DISTINCT NULLIF(project_plugins.plugin_name, '')) AS plugin_names,
                COUNT(DISTINCT NULLIF(project_plugins.plugin_path, '')) AS plugin_paths
            FROM ranked
            JOIN project_plugins ON project_plugins.project_id = ranked.id
            WHERE rn = 1
            """
        ).fetchone()

    groups = int(total_groups or 0)
    canonical_sample_refs = int(rep_metrics["sample_refs"] or 0)
    canonical_plugin_instances = int(rep_metrics["plugin_instances"] or 0)
    return {
        "groups": groups,
        "files": int(total_files or 0),
        "variant_files": max(0, int(total_files or 0) - groups),
        "duplicates": int(duplicate_groups or 0),
        "exact_duplicate_files": int(exact_duplicate_files or 0),
        "samples": int(unique_samples or 0),
        "sample_refs": canonical_sample_refs,
        "plugin_paths": int(plugin_stats["plugin_paths"] or 0),
        "plugin_names": int(plugin_stats["plugin_names"] or 0),
        "plugin_instances": canonical_plugin_instances,
        "versions": int(rep_metrics["versions"] or 0),
        "canonical_size": int(rep_metrics["canonical_size"] or 0),
        "physical_size": int(physical_size or 0),
        "canonical_time_seconds": float(rep_metrics["canonical_time_seconds"] or 0),
        "avg_plugins": (canonical_plugin_instances / groups) if groups else 0,
        "avg_samples": (canonical_sample_refs / groups) if groups else 0,
        "errors": int(errors or 0),
    }


def plugin_usage(limit: int = 12) -> list[tuple[str, int]]:
    with get_conn() as conn:
        rows = conn.execute(
            representative_cte()
            + """
            SELECT project_plugins.plugin_name AS name, COUNT(DISTINCT ranked.group_key) AS count
            FROM ranked
            JOIN project_plugins ON project_plugins.project_id = ranked.id
            WHERE rn = 1 AND project_plugins.plugin_name <> ''
            GROUP BY project_plugins.plugin_name
            ORDER BY count DESC, lower(project_plugins.plugin_name) ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [(row["name"], int(row["count"])) for row in rows]


def version_usage() -> list[tuple[str, int]]:
    with get_conn() as conn:
        rows = conn.execute(
            representative_cte()
            + """
            SELECT COALESCE(fl_version, 'Unknown') AS version, COUNT(*) AS count
            FROM ranked
            WHERE rn = 1
            GROUP BY COALESCE(fl_version, 'Unknown')
            ORDER BY count DESC, version ASC
            """
        ).fetchall()
    return [(row["version"], int(row["count"])) for row in rows]


def plugin_options() -> list[dict[str, str]]:
    options = [{"label": name, "value": name} for name, _count in plugin_usage(limit=200)]
    return options


def stat_card(title: str, value: Any, subtitle: str, accent: str, icon: str) -> dbc.Col:
    return dbc.Col(
        html.Div(
            [
                html.Div(title, className="stat-title"),
                html.Div(str(value), className="stat-value"),
                html.Div(subtitle or "\u00a0", className="stat-subtitle"),
            ],
            className=f"stat-card accent-{accent}",
        ),
        xs=6,
        lg=4,
        xl=2,
    )


def mini_metric(label: str, value: str, caption: str) -> html.Div:
    return html.Div(
        [
            html.Div(label, className="global-label"),
            html.Div(value, className="global-value"),
        ],
        className="global-metric",
    )


def global_stats_panel(stats: dict[str, Any]) -> dbc.Card:
    return dbc.Card(
        dbc.CardBody(
            [
                html.Div(
                    [
                        html.H2("Archive totals", className="panel-title"),
                    ],
                    className="panel-head",
                ),
                html.Div(
                    [
                        mini_metric("Total time", seconds_to_label(stats["canonical_time_seconds"]) or "", ""),
                        mini_metric("Plugin instances", compact_count(stats["plugin_instances"]), ""),
                        mini_metric("Sample refs", compact_count(stats["sample_refs"]), ""),
                        mini_metric("Avg plugins", f"{stats['avg_plugins']:.1f}", ""),
                        mini_metric("Avg samples", f"{stats['avg_samples']:.1f}", ""),
                        mini_metric("FL versions", compact_count(stats["versions"]), ""),
                        mini_metric("Canonical size", human_bytes(stats["canonical_size"]), ""),
                        mini_metric("All FLP size", human_bytes(stats["physical_size"]), ""),
                    ],
                    className="global-grid",
                ),
            ]
        ),
        className="global-card mt-3",
    )


def empty_figure(title: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text="No data yet", x=0.5, y=0.5, showarrow=False, font={"size": 18})
    fig.update_layout(
        template="plotly_dark",
        title=title,
        height=300,
        autosize=False,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 20, "r": 20, "t": 52, "b": 24},
    )
    return fig


def make_plugin_chart() -> go.Figure:
    usage = plugin_usage(limit=10)
    if not usage:
        return empty_figure("Plugin Usage")
    names = [name for name, _count in usage][::-1]
    counts = [count for _name, count in usage][::-1]
    fig = go.Figure(
        go.Bar(
            x=counts,
            y=names,
            orientation="h",
            marker={"color": "#4fd1c5", "line": {"color": "rgba(255,255,255,.18)", "width": 1}},
            hovertemplate="<b>%{y}</b><br>%{x} project families<extra></extra>",
        )
    )
    fig.update_layout(
        template="plotly_dark",
        title="Top Plugins",
        height=300,
        autosize=False,
        margin={"l": 148, "r": 24, "t": 52, "b": 36},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis_title="Project families",
        yaxis_title="",
        bargap=0.28,
        font={"color": "#e5edf5", "size": 12},
        uirevision="plugin-usage",
    )
    fig.update_xaxes(gridcolor="rgba(255,255,255,.08)", zeroline=False, rangemode="tozero")
    fig.update_yaxes(automargin=True)
    return fig


def make_version_chart() -> go.Figure:
    usage = version_usage()
    if not usage:
        return empty_figure("FL Versions")
    top = usage[:10]
    remaining = sum(count for _name, count in usage[10:])
    if remaining:
        top.append(("Other versions", remaining))
    labels = [name for name, _count in top][::-1]
    values = [count for _name, count in top][::-1]
    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker={"color": "#ffb703", "line": {"color": "rgba(255,255,255,.18)", "width": 1}},
            hovertemplate="<b>%{y}</b><br>%{x} project families<extra></extra>",
        )
    )
    fig.update_layout(
        template="plotly_dark",
        title="FL Studio Versions",
        height=300,
        autosize=False,
        margin={"l": 128, "r": 24, "t": 52, "b": 36},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis_title="Project families",
        yaxis_title="",
        bargap=0.28,
        font={"color": "#e5edf5", "size": 12},
        showlegend=False,
        uirevision="version-usage",
    )
    fig.update_xaxes(gridcolor="rgba(255,255,255,.08)", zeroline=False, rangemode="tozero")
    fig.update_yaxes(automargin=True)
    return fig


def metadata_badge(label: str, color: str = "secondary") -> dbc.Badge:
    return html.Span(label, className="metadata-pill")


def plugin_label(plugin: dict[str, Any]) -> str:
    return str(plugin.get("name") or plugin.get("display_name") or plugin.get("internal_name") or "Unnamed plugin")


def build_detail(group_key: str | None) -> list[Any]:
    if not group_key:
        return [
            dbc.Card(
                dbc.CardBody(
                    [
                        html.H2("Project detail", className="panel-title mb-1"),
                        html.Div("Select a project family", className="panel-note"),
                    ]
                ),
                className="detail-card",
            )
        ]

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM projects
            WHERE group_key = ? AND parse_error IS NULL
            ORDER BY datetime(COALESCE(modified, '1970-01-01')) DESC, size DESC
            """,
            (group_key,),
        ).fetchall()

    if not rows:
        return [dbc.Alert("That project group is no longer in the cache.", color="warning")]

    representative = rows[0]
    metadata = load_metadata_json(representative)
    project = metadata.get("project", {})
    header = metadata.get("header", {})
    names = metadata.get("names", {})
    paths = metadata.get("paths", {})
    plugins = metadata.get("plugins", [])
    fl_version = first_present(representative["fl_version"], project.get("fl_version"))
    channel_count = first_present(representative["channel_count"], header.get("channel_count"), "?")
    ppq = first_present(representative["ppq"], header.get("ppq"), "?")
    created_label = compact_datetime(first_present(representative["created_on"], project.get("created_on"))) or "Unknown"
    time_label = seconds_to_label(first_present(representative["time_spent_seconds"], project.get("time_spent_seconds"))) or "Unknown"
    tempo_label = format_tempo(first_present(representative["tempo_bpm"], project.get("tempo_bpm")), fl_version) or "Unknown"

    file_rows = [
        {
            "File": row["file_name"],
            "Modified": row["modified"],
            "Size": human_bytes(row["size"]),
            "SHA": str(row["sha256"])[:12],
            "Path": row["path"],
        }
        for row in rows
    ]
    plugin_rows = [
        {
            "Display": plugin.get("display_name") or "",
            "Plugin": plugin_label(plugin),
            "Vendor": plugin.get("vendor") or "",
            "Path": plugin.get("plugin_path") or "",
            "State": human_bytes(plugin.get("state_length")) if plugin.get("state_length") else "",
        }
        for plugin in plugins
    ]
    sample_rows = [{"Sample Path": sample} for sample in paths.get("sample_paths", [])]

    text_items = []
    for plugin in plugins:
        for value in plugin.get("embedded_strings", [])[:12]:
            text_items.append((plugin_label(plugin), value))
        for value in plugin.get("state_strings", [])[:12]:
            text_items.append((plugin_label(plugin), value))
        state_xml = plugin.get("state_xml")
        if state_xml:
            attrs = state_xml.get("attributes", {})
            preset = attrs.get("presetName") or attrs.get("versionString")
            if preset:
                text_items.append((plugin_label(plugin), f"{state_xml.get('root')}: {preset}"))

    overview = dbc.Card(
        dbc.CardBody(
            [
                html.Div(
                    [
                        html.H2(representative["group_name"], className="detail-title mb-1"),
                        html.Div(representative["path"], className="detail-path"),
                    ],
                    className="mb-3",
                ),
                html.Div(
                    [
                        metadata_badge(f"FL {fl_version or 'Unknown'}", "info"),
                        metadata_badge(f"{channel_count} channels", "primary"),
                        metadata_badge(f"PPQ {ppq}", "secondary"),
                        metadata_badge(f"{len(rows)} file(s)", "warning" if len(rows) > 1 else "secondary"),
                        metadata_badge(f"{len(paths.get('sample_paths', []))} samples", "success"),
                        metadata_badge(f"{len(plugins)} plugins", "danger"),
                    ],
                    className="mb-3",
                ),
                dbc.Row(
                    [
                        dbc.Col(html.Div([html.Div("Created", className="mini-label"), html.Div(created_label, className="mini-value")]), md=6),
                        dbc.Col(html.Div([html.Div("Time", className="mini-label"), html.Div(time_label, className="mini-value")]), md=3),
                        dbc.Col(html.Div([html.Div("Tempo", className="mini-label"), html.Div(tempo_label, className="mini-value")]), md=3),
                    ],
                    className="g-3",
                ),
                html.Hr(),
                html.Div("Display groups", className="mini-label mb-2"),
                html.Div(
                    [metadata_badge(name, "secondary") for name in names.get("display_group_names_or_parameter_blobs", [])]
                    or [html.Span("None found", className="text-secondary")],
                ),
            ]
        ),
        className="detail-card",
    )

    tabs = dbc.Tabs(
        [
            dbc.Tab(
                dash_table.DataTable(
                    data=file_rows,
                    columns=[{"name": column, "id": column} for column in ("File", "Modified", "Size", "SHA", "Path")],
                    page_size=6,
                    style_as_list_view=True,
                    style_table={"overflowX": "auto"},
                    style_cell={"backgroundColor": "transparent", "color": "#e5edf5", "border": "0", "fontFamily": "Inter, Segoe UI, sans-serif", "fontSize": "13px", "maxWidth": "440px", "overflow": "hidden", "textOverflow": "ellipsis"},
                    style_header={"backgroundColor": "rgba(255,255,255,.08)", "fontWeight": "700"},
                ),
                label="Files",
            ),
            dbc.Tab(
                dash_table.DataTable(
                    data=plugin_rows,
                    columns=[{"name": column, "id": column} for column in ("Display", "Plugin", "Vendor", "Path", "State")],
                    page_size=8,
                    style_as_list_view=True,
                    style_table={"overflowX": "auto"},
                    style_cell={"backgroundColor": "transparent", "color": "#e5edf5", "border": "0", "fontFamily": "Inter, Segoe UI, sans-serif", "fontSize": "13px", "maxWidth": "360px", "overflow": "hidden", "textOverflow": "ellipsis"},
                    style_header={"backgroundColor": "rgba(255,255,255,.08)", "fontWeight": "700"},
                ),
                label="Plugins",
            ),
            dbc.Tab(
                dash_table.DataTable(
                    data=sample_rows,
                    columns=[{"name": "Sample Path", "id": "Sample Path"}],
                    page_size=10,
                    style_as_list_view=True,
                    style_table={"overflowX": "auto"},
                    style_cell={"backgroundColor": "transparent", "color": "#e5edf5", "border": "0", "fontFamily": "Inter, Segoe UI, sans-serif", "fontSize": "13px", "maxWidth": "900px", "overflow": "hidden", "textOverflow": "ellipsis"},
                    style_header={"backgroundColor": "rgba(255,255,255,.08)", "fontWeight": "700"},
                ),
                label="Samples",
            ),
            dbc.Tab(
                html.Div(
                    [
                        dbc.ListGroup(
                            [
                                dbc.ListGroupItem(
                                    [
                                        html.Div(source, className="text-secondary small mb-1"),
                                        html.Div(value, className="text-break"),
                                    ],
                                    className="detail-list-item",
                                )
                                for source, value in text_items[:80]
                            ],
                            flush=True,
                        )
                        if text_items
                        else html.Div("No embedded plugin text found for the representative file.", className="text-secondary p-3")
                    ],
                    className="text-panel",
                ),
                label="Text",
            ),
        ],
        className="mt-3 detail-tabs",
    )

    return [overview, tabs]


def make_layout() -> dbc.Container:
    table_columns = [
        {"name": "Project Family", "id": "project"},
        {"name": "Latest File", "id": "latest_file"},
        {"name": "Modified", "id": "latest_modified"},
        {"name": "Files", "id": "files", "type": "numeric"},
        {"name": "Variants", "id": "variants", "type": "numeric"},
        {"name": "FL", "id": "fl_version"},
        {"name": "Channels", "id": "channels", "type": "numeric"},
        {"name": "PPQ", "id": "ppq", "type": "numeric"},
        {"name": "BPM", "id": "tempo", "type": "numeric"},
        {"name": "Samples", "id": "samples", "type": "numeric"},
        {"name": "Plugins", "id": "plugins", "type": "numeric"},
        {"name": "Time", "id": "time_spent"},
        {"name": "Size", "id": "size"},
    ]
    table_style_cell = {
        "backgroundColor": "transparent",
        "color": "#e8eef7",
        "border": "0",
        "fontFamily": "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
        "fontSize": "13px",
        "lineHeight": "18px",
        "textAlign": "left",
        "maxWidth": "280px",
        "overflow": "hidden",
        "textOverflow": "ellipsis",
        "padding": "9px 10px",
        "height": "38px",
        "minHeight": "38px",
    }
    table_style_header = {
        "backgroundColor": "#1b2430",
        "color": "#f8fafc",
        "fontWeight": "700",
        "border": "0",
        "fontSize": "12px",
        "height": "38px",
        "padding": "9px 10px",
    }
    return dbc.Container(
        [
            dcc.Interval(id="refresh-interval", interval=1500, n_intervals=0, disabled=True),
            dcc.Store(id="catalog-revision", data={"rev": 0, "active": False}),
            html.Button(id="upload-started-button", n_clicks=0, style={"display": "none"}),
            html.Div(
                [
                    html.Div(
                        [
                            html.H1("FL Stats", className="app-title"),
                            html.Div("FL Studio project catalog", className="app-subtitle"),
                        ],
                        className="brand-block",
                    ),
                    html.Div(
                        [
                            dbc.Button(
                                "Scan default folder",
                                id="scan-default-button",
                                color="warning",
                                className="primary-action",
                            ),
                            dbc.Button("Refresh", id="refresh-button", color="secondary", outline=True, className="secondary-action"),
                            dbc.Button("Clear cache", id="clear-cache-button", color="danger", outline=True, className="secondary-action"),
                        ],
                        className="top-actions",
                    ),
                ],
                className="app-topbar",
            ),
            html.Div(
                [
                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    html.Label("Folder path", className="field-label", htmlFor="folder-path"),
                                    dbc.InputGroup(
                                        [
                                            dbc.Input(id="folder-path", value=DEFAULT_SCAN_PATH, debounce=True),
                                            dbc.Button("Scan", id="scan-button", color="info"),
                                        ]
                                    ),
                                ],
                                lg=8,
                            ),
                            dbc.Col(
                                [
                                    html.Div(id="folder-uploader-host", className="folder-upload-host"),
                                ],
                                lg=4,
                            ),
                        ],
                        className="g-3 align-items-end",
                    ),
                ],
                className="control-panel",
            ),
            html.Div(id="scan-status", className="scan-status-slot"),
            dbc.Progress(id="scan-progress", value=0, striped=True, animated=True, className="scan-progress"),
            dbc.Row(id="stat-cards", className="g-3 metric-row"),
            dbc.Card(
                dbc.CardBody(
                    [
                        html.Div(
                            [
                                html.H2("Project families", className="panel-title"),
                            ],
                            className="panel-head mb-3",
                        ),
                        dbc.Row(
                            [
                                dbc.Col(dbc.Input(id="search-input", placeholder="Search projects, paths, comments, plugins", debounce=True), lg=7),
                                dbc.Col(dcc.Dropdown(id="plugin-filter", placeholder="Plugin", clearable=True, className="dbc-dropdown"), lg=3),
                                dbc.Col(dbc.Button("Reset", id="reset-filters", color="secondary", outline=True, className="w-100"), lg=2),
                            ],
                            className="g-2 mb-3",
                        ),
                        dash_table.DataTable(
                            id="project-table",
                            columns=table_columns,
                            data=[],
                            page_current=0,
                            page_size=PAGE_SIZE,
                            page_action="custom",
                            sort_action="custom",
                            sort_mode="single",
                            sort_by=[{"column_id": "latest_modified", "direction": "desc"}],
                            row_selectable=False,
                            cell_selectable=True,
                            style_as_list_view=True,
                            style_table={"overflowX": "auto", "minHeight": "700px"},
                            style_cell=table_style_cell,
                            style_header=table_style_header,
                            style_cell_conditional=[
                                {"if": {"column_id": column}, "textAlign": "right"}
                                for column in ("files", "variants", "channels", "ppq", "tempo", "samples", "plugins", "time_spent", "size")
                            ],
                            style_header_conditional=[
                                {"if": {"column_id": column}, "textAlign": "right"}
                                for column in ("files", "variants", "channels", "ppq", "tempo", "samples", "plugins", "time_spent", "size")
                            ],
                            style_data_conditional=list(BASE_PROJECT_TABLE_STYLES),
                            tooltip_delay=300,
                            tooltip_duration=None,
                        ),
                        html.Div(id="table-page-status", className="table-page-status"),
                    ]
                ),
                className="table-card mt-3",
            ),
            html.Div(id="project-detail", className="mt-3"),
            html.Div(id="global-stats", className="analysis-offset"),
            dbc.Row(
                [
                    dbc.Col(
                        html.Div(
                            dcc.Graph(
                                id="plugin-chart",
                                config={"displayModeBar": False, "responsive": False},
                                style={"height": "300px"},
                                className="chart-graph",
                            ),
                            className="chart-card",
                        ),
                        lg=7,
                    ),
                    dbc.Col(
                        html.Div(
                            dcc.Graph(
                                id="version-chart",
                                config={"displayModeBar": False, "responsive": False},
                                style={"height": "300px"},
                                className="chart-graph",
                            ),
                            className="chart-card",
                        ),
                        lg=5,
                    ),
                ],
                className="g-3 mt-3 mb-5 chart-row",
            ),
        ],
        fluid=True,
        className="app-shell",
    )


init_db()
repair_cached_group_names()
repair_cached_project_metrics()
prune_scan_errors()
rebuild_side_tables_if_needed()
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.SLATE, dbc.icons.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title="FL Stats",
    update_title=None,
)
server = app.server
register_upload_route(server)
app.layout = make_layout


@app.callback(
    Output("scan-status", "children"),
    Output("scan-progress", "value"),
    Output("scan-progress", "label"),
    Output("scan-progress", "color"),
    Output("scan-progress", "animated"),
    Output("catalog-revision", "data"),
    Output("refresh-interval", "disabled"),
    Input("scan-default-button", "n_clicks"),
    Input("scan-button", "n_clicks"),
    Input("clear-cache-button", "n_clicks"),
    Input("upload-started-button", "n_clicks"),
    Input("refresh-interval", "n_intervals"),
    State("folder-path", "value"),
    State("catalog-revision", "data"),
    prevent_initial_call=False,
)
def scan_controls(default_clicks, scan_clicks, clear_clicks, upload_clicks, _tick, folder_value, catalog):
    catalog = catalog or {"rev": 0, "active": False}
    revision = int(catalog.get("rev") or 0)
    triggered = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if triggered in {"scan-button", "scan-default-button"}:
        folder = Path(DEFAULT_SCAN_PATH if triggered == "scan-default-button" else (folder_value or "")).expanduser()
        if not folder.exists() or not folder.is_dir():
            return dbc.Alert(f"Folder not found: {folder}", color="warning"), 0, "Idle", "secondary", False, no_update, True
        paths = iter_flp_files(folder)
        if not paths:
            return dbc.Alert(f"No .flp files found under {folder}", color="warning"), 0, "Idle", "secondary", False, no_update, True
        started, message = start_scan(paths, str(folder), "folder")
        color = "info" if started else "warning"
        next_catalog = {"rev": revision, "active": True} if started else no_update
        return dbc.Alert(message, color=color), 0, "Starting", color, started, next_catalog, not started

    if triggered == "clear-cache-button":
        snapshot = job_snapshot()
        if snapshot.get("active"):
            return dbc.Alert("Wait for the current scan to finish before clearing the cache.", color="warning"), no_update, no_update, no_update, no_update, no_update, False
        clear_dashboard_cache()
        return (
            dbc.Alert("Dashboard cache cleared. Your original FLP files were not touched.", color="danger"),
            0,
            "Cleared",
            "secondary",
            False,
            {"rev": revision + 1, "active": False},
            True,
        )

    snapshot = job_snapshot()
    total = int(snapshot.get("total") or 0)
    done = int(snapshot.get("done") or 0)
    percent = int((done / total) * 100) if total else 0
    active = bool(snapshot.get("active"))
    color = "info" if active else ("success" if total else "secondary")
    label = f"{done}/{total}" if total else "Idle"
    current = snapshot.get("current")
    status = dbc.Alert(
        [
            html.Strong(snapshot.get("message", "Idle")),
            html.Div(
                f"OK: {snapshot.get('ok', 0)} | Errors: {snapshot.get('errors', 0)}"
                + (f" | Current: {current}" if current else ""),
                className="small mt-1",
            ),
        ],
        color=color,
        className="scan-alert",
    )
    next_catalog: Any = no_update
    was_active = bool(catalog.get("active"))
    if active and not was_active:
        next_catalog = {"rev": revision, "active": True}
    elif not active and was_active:
        next_catalog = {"rev": revision + 1, "active": False}
    return status, percent, label, color, active, next_catalog, not active


@app.callback(
    Output("stat-cards", "children"),
    Output("global-stats", "children"),
    Output("plugin-chart", "figure"),
    Output("version-chart", "figure"),
    Output("plugin-filter", "options"),
    Input("catalog-revision", "data"),
    Input("refresh-button", "n_clicks"),
)
def refresh_overview(_catalog, _refresh):
    stats = stats_snapshot()
    cards = [
        stat_card("Project Families", compact_count(stats["groups"]), "", "teal", "bi-collection-play"),
        stat_card("Physical FLPs", compact_count(stats["files"]), f"{compact_count(stats['variant_files'])} backups", "amber", "bi-files"),
        stat_card("Duplicate Groups", compact_count(stats["duplicates"]), "", "rose", "bi-intersect"),
        stat_card("Unique Samples", compact_count(stats["samples"]), "", "green", "bi-music-note-list"),
        stat_card("Unique Plugins", compact_count(stats["plugin_names"]), f"{compact_count(stats['plugin_paths'])} paths", "blue", "bi-plugin"),
        stat_card("Scan Errors", compact_count(stats["errors"]), "", "red", "bi-exclamation-triangle"),
    ]
    return cards, global_stats_panel(stats), make_plugin_chart(), make_version_chart(), plugin_options()


@app.callback(
    Output("project-table", "data"),
    Output("project-table", "page_count"),
    Input("project-table", "page_current"),
    Input("project-table", "page_size"),
    Input("project-table", "sort_by"),
    Input("search-input", "value"),
    Input("plugin-filter", "value"),
    Input("catalog-revision", "data"),
    Input("refresh-interval", "n_intervals"),
)
def update_table(page_current, page_size, sort_by, search, plugin, _catalog, _tick):
    triggered = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if triggered == "refresh-interval" and not job_snapshot().get("active"):
        raise PreventUpdate
    return query_group_rows(page_current or 0, page_size or PAGE_SIZE, sort_by, search, plugin)


@app.callback(
    Output("search-input", "value"),
    Output("plugin-filter", "value"),
    Input("reset-filters", "n_clicks"),
    prevent_initial_call=True,
)
def reset_filters(_clicks):
    return "", None


@app.callback(
    Output("project-table", "page_current", allow_duplicate=True),
    Input("project-table", "sort_by"),
    Input("search-input", "value"),
    Input("plugin-filter", "value"),
    prevent_initial_call=True,
)
def reset_table_page(_sort_by, _search, _plugin):
    return 0


@app.callback(
    Output("table-page-status", "children"),
    Input("project-table", "page_current"),
    Input("project-table", "page_count"),
    Input("project-table", "data"),
)
def update_table_page_status(page_current, page_count, rows):
    pages = max(1, int(page_count or 1))
    current = min(max(0, int(page_current or 0)), pages - 1)
    visible = len(rows or [])
    return f"Page {current + 1} of {pages} · {visible} visible rows"


@app.callback(
    Output("project-table", "style_data_conditional"),
    Input("project-table", "active_cell"),
    State("project-table", "data"),
)
def update_project_table_styles(active_cell, rows):
    styles = list(BASE_PROJECT_TABLE_STYLES)
    if not active_cell or not rows:
        return styles
    row_index = active_cell.get("row")
    if row_index is None or row_index >= len(rows):
        return styles
    styles.append(
        {
            "if": {"row_index": row_index},
            "backgroundColor": "#263442",
            "color": "#f8fafc",
        }
    )
    return styles


@app.callback(
    Output("project-detail", "children"),
    Input("project-table", "active_cell"),
    State("project-table", "data"),
)
def update_detail(active_cell, rows):
    if not active_cell or not rows:
        return build_detail(None)
    row_index = active_cell.get("row")
    if row_index is None or row_index >= len(rows):
        return build_detail(None)
    return build_detail(rows[row_index].get("group_key"))


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050, use_reloader=False)
