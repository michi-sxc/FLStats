# FLStats

Dashboard for browsing FL Studio `.flp` project metadata and showing global FL Studio statistics.
As the `.flp` file format is a proprietary binary, there exists no official documentation so expect a lot of bugs. 

## Requirements

- Python 3
- Packages listed in `requirements.txt` (Dash, dash-bootstrap-components)
- .flp files tested from FL Studio 12.0.3 up to 25.2.5

## Run

```powershell
python flp_dashboard.py
```

Then open `http://127.0.0.1:8050` in a browser.

On Windows, you can also run `launch_dashboard.cmd` or `launch_dashboard.ps1`. The launcher checks dependencies, starts the server, and opens the dashboard automatically.
You can stop the server with CTRL+C in the terminal.

## Storage

Metadata is parsed on the server and stored in `flp_dashboard.sqlite3`. The browser receives table pages, chart summaries, and the project detail panel.
It is recommended to use the local file scanning instead of direct file uploads in the browser.

## Ingestion

- `Folder` input with `Scan`: reads files from a path on the server. FLP bytes stay on the server.
- `Folder upload`: copies `.flp` files into `.flp_dashboard_uploads` and parses them from there. Use this when the browser runs on a different machine than the server.

## Duplicate Handling

The table groups files into a project family by collapsing filename patterns such as:

- `backup`, `bak`, `autosave`, `recovered`
- `v2`, `version 3`, `rev 4`
- date and time suffixes
- `copy`, `final`, `master`, `mixdown`

The detail panel lists every file in a family, including versions and duplicate hashes.

Stats count one file per project family so backups and prior versions do not inflate plugin, sample, time, and version totals. File cards still report the count of `.flp` files and variants on disk.
Duplicate detection is not perfect as some backups do not get detected (e.g. when updating to a newer FL Studio version).

## Files

- `flp_dashboard.py` - Dash app and SQLite ingestion/query layer
- `flp_metadata.py` - FLP metadata parser
- `assets/flp_dashboard.css` - styling
- `assets/folder_upload.js` - folder upload helper
- `launch_flp_dashboard.cmd` - Windows launcher entry point
- `launch_flp_dashboard.ps1` - PowerShell launcher script
- `requirements.txt` - Python dependencies
