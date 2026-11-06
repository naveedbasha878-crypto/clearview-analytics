# Clearview Analytics

A local Flask app for uploading, cleaning, exploring, visualizing, and exporting tabular datasets.

## Install on Windows

Run `build_windows.ps1` in PowerShell to build a per-user Windows setup program. It creates `installer-output\ClearviewAnalyticsSetup.exe`, which installs Clearview in your user Programs folder with Start Menu and optional desktop shortcuts. Windows installers require Inno Setup 6; install it once with `winget install --id JRSoftware.InnoSetup --exact --scope user`, then run the build script. Build files are isolated under `build-desktop` and `dist-desktop`.

The installed app opens in its own window. On first launch, create your local sign-in and optionally enter your OpenAI API key in the setup screen. These settings are saved under `%LOCALAPPDATA%\Clearview Analytics\.env`, outside the installation directory. The API key is optional; local stories and insights work without it. Keep your Windows account private because the settings file contains credentials.

To build:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
.\build_windows.ps1
```

The build installs dependencies from `requirements-build.txt` and creates an unsigned installer. Windows may show a SmartScreen warning because the installer is not code-signed.

## Run locally

Requires Python 3.10 or later.

For direct Python development runs, create `%LOCALAPPDATA%\Clearview Analytics\.env` once using the commands below, then enter your username, password, a random Flask secret, and (optionally) your OpenAI API key. The app loads this per-user config automatically. It stays outside this OneDrive workspace and is excluded from Git. The file is still sensitive; do not share it. Without an API key, local stories and insights still work.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$configDir = Join-Path $env:LOCALAPPDATA "Clearview Analytics"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
Copy-Item .env.example (Join-Path $configDir ".env")
notepad (Join-Path $configDir ".env")
# Replace the placeholders and optionally set OPENAI_API_KEY, then save.
python app.py
```

Open `http://127.0.0.1:5000`. The app intentionally has no default login credentials; configure `APP_USERNAME` and `APP_PASSWORD` in the per-user `.env` before starting it. Environment variables already set by Windows take precedence over the config file.

## Open on a phone from anywhere

The Windows installer is not a mobile app. To use Clearview from a phone, deploy it as a website. The included `render.yaml` is a starting point for Render:

1. Create a **private** GitHub repository and upload the project files. Do not upload `.env` files or real API keys.
2. In Render, create a Blueprint from that repository and set `APP_USERNAME` and `APP_PASSWORD` when prompted. Use a unique, strong password. The blueprint generates `FLASK_SECRET_KEY` and enables HTTPS-only session cookies.
3. After deployment, open the Render service URL on your phone and sign in. You can bookmark it or add it to your phone's home screen.

The hosted app refuses to start unless its credentials and session secret are configured, so the first-run account creation page cannot be claimed by a visitor. Keep the GitHub repository private and do not share the app URL or credentials. The app is designed for one user; uploaded datasets are held in server memory, are sent to the hosting provider, and can be lost when a free service sleeps or restarts. Do not upload confidential or regulated data. A private repository does not make the deployed website private.

## Optional integrations

- **OpenAI insights and recommendations:** set `OPENAI_API_KEY` in `%LOCALAPPDATA%\Clearview Analytics\.env`; optionally set `OPENAI_MODEL` (defaults to `gpt-4o-mini`). Without a key, stories and insights use local deterministic analysis. OpenAI requests contain aggregates and category summaries, not raw rows. Review the data and your organization's AI policy before enabling this integration.
- **PostgreSQL SQL uploads:** set `DATABASE_URL`, for example `postgresql://user:password@host:5432/database`. SQL uploads must contain exactly one `SELECT` or `WITH` query. The app runs it in a read-only transaction with a 10-second statement timeout and a 100,000-row limit.
- **Secure cookies:** set `COOKIE_SECURE=true` when serving the app over HTTPS.

## Features

- CSV, XLSX, JSON, delimited TXT, and read-only PostgreSQL query uploads; drag and drop is supported.
- Dataset summary, missing-data and quality checks, descriptive statistics, search, column filters, and a data preview.
- Explicit cleaning actions for nulls, duplicates, blank rows/columns, whitespace, column names, data types, and IQR/Z-score outliers.
- Interactive Plotly charts (bar, pie, histogram, line, scatter, area, box, heatmap, bubble, treemap, funnel, waterfall, donut) with color selection.
- Local four-stage business storytelling plus rule-based insights and recommendations (descriptive, diagnostic, predictive, and prescriptive) without an API key; OpenAI-generated insights are optional. Local forecasts are directional extrapolations and are only generated when enough dated observations are available.
- CSV, Excel, PDF, PowerPoint, and browser-rendered chart-image downloads.

The in-memory dataset cache is intended for a single local app process; restarting the server clears uploaded datasets. Do not expose the development server directly to the public internet. Use HTTPS and a production WSGI server and review your authentication, session, and deployment settings before hosting this app for other users.
