# FLP Observatory Dashboard

Run:

```powershell
python flp_dashboard.py
```

Then open:

```text
http://127.0.0.1:8050
```

Or double-click:

```text
launch_flp_dashboard.cmd
```

The launcher installs missing dependencies, starts the local server, and opens the dashboard.

## How It Stores Data

Parsed metadata is stored server-side in `flp_dashboard.sqlite3`. The browser only receives the current page of table rows, chart summaries, and the selected project detail panel.

## Ingesting Projects

- Use the `Folder` input and `Scan` button for local folders. This is the fastest path because FLP bytes never pass through the browser.
- Use `Folder upload` when accessing the dashboard from another machine/browser. Uploaded `.flp` files are copied into `.flp_dashboard_uploads` and parsed from there.

## Duplicate Handling

The main table groups files into a project family. It collapses common backup/version filename patterns such as:

- `backup`, `bak`, `autosave`, `recovered`
- `v2`, `version 3`, `rev 4`
- date/time suffixes
- `copy`, `final`, `master`, `mixdown`

The detail panel still shows every physical file in that family, including older versions and exact duplicate hashes.

Global stats use one representative/latest FLP per project family so backups and earlier versions do not inflate plugin, sample, time, and version totals. The physical-file cards still show how many raw `.flp` files and variants are in the archive.

## Useful Files

- `flp_dashboard.py` - Dash app and SQLite ingestion/query layer
- `flp_metadata.py` - FLP metadata parser
- `assets/flp_dashboard.css` - dashboard styling
- `assets/folder_upload.js` - native folder-upload helper
- `launch_flp_dashboard.cmd` - one-click Windows launcher
