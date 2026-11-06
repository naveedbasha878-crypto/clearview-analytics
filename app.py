import io
import json
import os
import re
import secrets
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import sqlparse
from dotenv import load_dotenv, set_key
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from sqlalchemy import create_engine, text
from werkzeug.utils import secure_filename


RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
local_app_data = os.environ.get("LOCALAPPDATA")
user_config_dir = (
    Path(local_app_data) / "Clearview Analytics"
    if local_app_data
    else Path.home() / ".config" / "clearview-analytics"
)
load_dotenv(user_config_dir / ".env", override=False)

hosted_mode = os.environ.get("CLEARVIEW_HOSTED_MODE", "").lower() == "true"
public_demo_mode = hosted_mode and os.environ.get("CLEARVIEW_PUBLIC_DEMO", "").lower() == "true"
if hosted_mode:
    required_hosted_settings = ("APP_USERNAME", "APP_PASSWORD", "FLASK_SECRET_KEY")
    missing_hosted_settings = [name for name in required_hosted_settings if not os.environ.get(name)]
    if missing_hosted_settings:
        raise RuntimeError(
            "Hosted mode requires these environment variables: "
            + ", ".join(missing_hosted_settings)
        )

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".json", ".sql", ".txt"}
MAX_ROWS = 100_000
datasets = {}

app = Flask(
    __name__,
    template_folder=str(RESOURCE_DIR / "templates"),
    static_folder=str(RESOURCE_DIR / "static"),
)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def current_dataset():
    dataset_id = session.get("dataset_id")
    return datasets.get(dataset_id) if dataset_id else None


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def protect_routes():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint != "login":
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        if not secrets.compare_digest(supplied, session.get("csrf_token", "")):
            return jsonify(error="Your session expired. Refresh the page and try again."), 400

    if request.endpoint in {"login", "setup", "static"}:
        return None
    if public_demo_mode:
        return None
    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify(error="Please sign in to continue."), 401
        return redirect(url_for("login"))
    return None


def require_dataset():
    data = current_dataset()
    if data is None:
        raise ValueError("Upload a dataset before using this feature.")
    return data["frame"]


def json_value(value):
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def type_guess(series):
    values = series.dropna()
    if values.empty:
        return "string"
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "float"
    text_values = values.astype(str).str.strip()
    lowered = text_values.str.lower()
    if lowered.isin({"true", "false", "yes", "no", "y", "n"}).mean() >= 0.8:
        return "boolean"
    numeric = pd.to_numeric(text_values.str.replace(",", "", regex=False), errors="coerce")
    if numeric.notna().mean() >= 0.8:
        return "integer" if (numeric.dropna() % 1 == 0).all() else "float"
    parsed_dates = pd.to_datetime(text_values, errors="coerce", format="mixed")
    if parsed_dates.notna().mean() >= 0.8:
        return "date"
    return "string"


def is_identifier_column(name):
    normalized = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return bool(
        re.search(r"(?:^|_)(?:id|phone|mobile|telephone|zip|postal(?:_code)?)(?:_|$)", normalized)
        or normalized.endswith(("id", "phone", "mobile", "telephone", "zipcode", "postalcode"))
    )


def date_format_counts(series):
    result = {"DD-MM-YYYY": 0, "MM-DD-YYYY": 0, "YYYY-MM-DD": 0}
    for value in series.dropna().astype(str).head(500):
        value = value.strip()
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
            result["YYYY-MM-DD"] += 1
        elif re.fullmatch(r"\d{1,2}-\d{1,2}-\d{4}", value):
            first, second = (int(part) for part in value.split("-")[:2])
            if first > 12:
                result["DD-MM-YYYY"] += 1
            elif second > 12:
                result["MM-DD-YYYY"] += 1
            else:
                result["DD-MM-YYYY"] += 1
    return result


def dataset_report(df):
    columns = []
    numeric = df.select_dtypes(include="number")
    for name in df.columns:
        series = df[name]
        non_null = series.dropna()
        guessed = "string" if is_identifier_column(name) else type_guess(series)
        actual = (
            "integer"
            if pd.api.types.is_integer_dtype(series)
            else "float"
            if pd.api.types.is_float_dtype(series)
            else "boolean"
            if pd.api.types.is_bool_dtype(series)
            else "date"
            if pd.api.types.is_datetime64_any_dtype(series)
            else "string"
        )
        as_text = non_null.astype(str).str.strip()
        numeric_ratio = pd.to_numeric(as_text, errors="coerce").notna().mean() if len(as_text) else 0
        mixed = actual == "string" and 0.05 < numeric_ratio < 0.95
        invalid_email = 0
        if "email" in str(name).lower():
            invalid_email = int(
                (~as_text.str.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", na=False)).sum()
            )
        invalid_phone = 0
        if "phone" in str(name).lower() or "mobile" in str(name).lower():
            digits = as_text.str.replace(r"\D", "", regex=True)
            invalid_phone = int((~digits.str.len().between(7, 15)).sum())
        special = int(as_text.str.contains(r"[^A-Za-z0-9\s.,_\-@/+()]", regex=True).sum())
        columns.append(
            {
                "name": str(name),
                "dtype": str(series.dtype),
                "guessed_type": guessed,
                "type_mismatch": guessed != actual,
                "missing": int(series.isna().sum()),
                "unique": int(series.nunique(dropna=True)),
                "negative": int((pd.to_numeric(series, errors="coerce") < 0).sum()),
                "invalid_email": invalid_email,
                "invalid_phone": invalid_phone,
                "special_characters": special,
                "mixed_types": bool(mixed),
                "date_formats": date_format_counts(series),
            }
        )

    numeric_stats = {}
    for name in numeric.columns:
        values = numeric[name].dropna()
        if values.empty:
            continue
        mode = values.mode()
        numeric_stats[str(name)] = {
            "mean": json_value(values.mean()),
            "median": json_value(values.median()),
            "mode": json_value(mode.iloc[0]) if not mode.empty else None,
            "std": json_value(values.std()),
            "variance": json_value(values.var()),
            "min": json_value(values.min()),
            "max": json_value(values.max()),
            "q1": json_value(values.quantile(0.25)),
            "q2": json_value(values.quantile(0.5)),
            "q3": json_value(values.quantile(0.75)),
        }

    outliers = {}
    for name in numeric.columns:
        values = numeric[name].dropna()
        if len(values) < 3:
            continue
        q1, q3 = values.quantile([0.25, 0.75])
        spread = q3 - q1
        iqr_count = int(((values < q1 - 1.5 * spread) | (values > q3 + 1.5 * spread)).sum())
        std = values.std()
        z_count = int((((values - values.mean()).abs() / std) > 3).sum()) if std else 0
        outliers[str(name)] = {"IQR": iqr_count, "Z Score": z_count}

    categorical = {}
    for name in df.select_dtypes(exclude="number").columns:
        top = df[name].dropna().astype(str).value_counts().head(5)
        if not top.empty:
            categorical[str(name)] = [
                {"value": str(value), "count": int(count)} for value, count in top.items()
            ]

    return {
        "rows": int(len(df)),
        "columns_count": int(len(df.columns)),
        "missing": int(df.isna().sum().sum()),
        "duplicate_rows": int(df.duplicated().sum()),
        "blank_rows": int(df.replace(r"^\s*$", pd.NA, regex=True).isna().all(axis=1).sum()),
        "blank_columns": int(df.replace(r"^\s*$", pd.NA, regex=True).isna().all(axis=0).sum()),
        "columns": columns,
        "numeric_stats": numeric_stats,
        "outliers": outliers,
        "categorical_top": categorical,
    }


def profile_payload(df):
    report = dataset_report(df)
    filter_options = {}
    for name in df.columns:
        if df[name].nunique(dropna=True) <= 30:
            values = df[name].dropna().astype(str).value_counts().index.tolist()
            if values:
                filter_options[str(name)] = values
    return {
        "filename": current_dataset()["filename"],
        "summary": report,
        "filter_options": filter_options,
        "column_names": [str(column) for column in df.columns],
        "preview": [
            {str(key): json_value(value) for key, value in row.items()}
            for row in df.head(40).to_dict(orient="records")
        ],
        "csrf_token": csrf_token(),
    }


def read_uploaded_file(upload):
    filename = secure_filename(upload.filename or "dataset")
    extension = Path(filename).suffix.lower()
    if public_demo_mode and extension == ".sql":
        raise ValueError("SQL uploads are disabled in the public demo.")
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Supported file types are CSV, XLSX, JSON, SQL, and TXT.")
    raw = upload.read()
    if not raw:
        raise ValueError("The uploaded file is empty.")

    if extension == ".csv":
        def read_csv(encoding):
            header = pd.read_csv(io.BytesIO(raw), encoding=encoding, nrows=0)
            identifier_columns = {name: "string" for name in header.columns if is_identifier_column(name)}
            return pd.read_csv(io.BytesIO(raw), encoding=encoding, dtype=identifier_columns)

        try:
            df = read_csv("utf-8-sig")
        except UnicodeDecodeError:
            df = read_csv("cp1252")
    elif extension == ".xlsx":
        df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")
    elif extension == ".json":
        try:
            df = pd.read_json(io.BytesIO(raw))
        except ValueError:
            df = pd.read_json(io.BytesIO(raw), lines=True)
    elif extension == ".txt":
        sample = raw[:8192].decode("utf-8", errors="replace")
        try:
            import csv

            separator = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            separator = "\t" if "\t" in sample else ","
        try:
            df = pd.read_csv(io.BytesIO(raw), sep=separator, encoding="utf-8", engine="python")
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(raw), sep=separator, encoding="cp1252", engine="python")
    else:
        query = raw.decode("utf-8-sig")
        df = read_only_sql(query)

    if len(df) > MAX_ROWS:
        raise ValueError(f"Dataset has more than {MAX_ROWS:,} rows; reduce it before uploading.")
    if df.empty and len(df.columns) == 0:
        raise ValueError("No tabular data was found in the uploaded file.")
    for column in df.columns:
        nested_values = df[column].map(lambda value: isinstance(value, (dict, list, tuple)))
        if nested_values.any():
            df.loc[nested_values, column] = df.loc[nested_values, column].map(
                lambda value: json.dumps(value, ensure_ascii=True, default=str)
            )
    unique_names = []
    seen_names = set()
    for column in df.columns:
        base = str(column)
        candidate, suffix = base, 2
        while candidate in seen_names:
            candidate = f"{base}.{suffix}"
            suffix += 1
        unique_names.append(candidate)
        seen_names.add(candidate)
    df.columns = unique_names
    for column in df.columns:
        if is_identifier_column(column):
            df[column] = df[column].astype("string")
    return df, filename


def read_only_sql(query):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("Set DATABASE_URL to a PostgreSQL connection string before uploading a .sql file.")
    statements = [sqlparse.format(statement, strip_comments=True).strip() for statement in sqlparse.split(query)]
    statements = [statement for statement in statements if statement]
    if len(statements) != 1 or not re.match(r"^(SELECT|WITH)\b", statements[0], re.IGNORECASE):
        raise ValueError("SQL uploads must contain exactly one SELECT query (WITH queries are allowed).")

    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '10s'")
                result = connection.execute(text(statements[0]))
                if not result.returns_rows:
                    raise ValueError("The SQL query did not return tabular data.")
                rows = result.fetchmany(MAX_ROWS + 1)
                if len(rows) > MAX_ROWS:
                    raise ValueError(f"SQL query returned more than {MAX_ROWS:,} rows.")
                frame = pd.DataFrame(rows, columns=result.keys())
                transaction.commit()
                return frame
            except Exception:
                transaction.rollback()
                raise
    finally:
        engine.dispose()


def filtered_frame(df, payload):
    frame = df
    search = str(payload.get("search", "")).strip()
    column = payload.get("column", "*")
    if search:
        searchable = frame.columns if column == "*" or column not in frame.columns else [column]
        mask = pd.Series(False, index=frame.index)
        for name in searchable:
            mask |= frame[name].astype("string").str.contains(search, case=False, na=False, regex=False)
        frame = frame.loc[mask]

    for name, value in (payload.get("filters") or {}).items():
        if name not in frame.columns or value in (None, "", "All"):
            continue
        if isinstance(value, dict) and pd.api.types.is_numeric_dtype(frame[name]):
            low, high = value.get("min"), value.get("max")
            numeric = pd.to_numeric(frame[name], errors="coerce")
            if low is not None:
                frame = frame.loc[numeric >= float(low)]
            if high is not None:
                frame = frame.loc[numeric <= float(high)]
        elif isinstance(value, dict):
            if value.get("contains") is not None:
                needle = str(value["contains"]).strip()
                if needle:
                    frame = frame.loc[
                        frame[name].astype("string").str.contains(needle, case=False, na=False, regex=False)
                    ]
            else:
                low, high = value.get("min"), value.get("max")
                formats = date_format_counts(frame[name])
                dayfirst = formats["DD-MM-YYYY"] >= formats["MM-DD-YYYY"]
                dates = pd.to_datetime(frame[name], errors="coerce", format="mixed", dayfirst=dayfirst, utc=True)
                if low:
                    frame = frame.loc[dates >= pd.to_datetime(low, utc=True)]
                if high:
                    frame = frame.loc[dates <= pd.to_datetime(high, utc=True)]
        else:
            frame = frame.loc[frame[name].astype(str) == str(value)]
    return frame


def response_error(error, status=400):
    return jsonify(error=str(error)), status


@app.route("/login", methods=["GET", "POST"])
def login():
    if public_demo_mode:
        return redirect(url_for("home"))
    if not os.environ.get("APP_USERNAME") or not os.environ.get("APP_PASSWORD"):
        return redirect(url_for("setup"))
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", "")):
            return render_template("login.html", error="Your session expired. Refresh and try again.", token=csrf_token()), 400
        username = os.environ.get("APP_USERNAME")
        password = os.environ.get("APP_PASSWORD")
        if not username or not password:
            return render_template(
                "login.html",
                error="Configure APP_USERNAME and APP_PASSWORD in your environment to enable sign-in.",
                token=csrf_token(),
            ), 503
        supplied_user = request.form.get("username", "")
        supplied_password = request.form.get("password", "")
        if secrets.compare_digest(supplied_user, username) and secrets.compare_digest(supplied_password, password):
            session.clear()
            session["authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = request.form.get("remember") == "on"
            return redirect(url_for("home"))
        return render_template("login.html", error="The username or password is incorrect.", token=csrf_token()), 401
    if session.get("authenticated"):
        return redirect(url_for("home"))
    return render_template("login.html", token=csrf_token(), error=None)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if public_demo_mode:
        return redirect(url_for("home"))
    if os.environ.get("APP_USERNAME") and os.environ.get("APP_PASSWORD"):
        return redirect(url_for("login"))
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", "")):
            return render_template("setup.html", error="Your session expired. Refresh and try again.", token=csrf_token()), 400
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        api_key = request.form.get("openai_api_key", "").strip()
        if not username or not password:
            return render_template("setup.html", error="Enter a username and password.", token=csrf_token()), 400
        if len(password) < 8:
            return render_template("setup.html", error="Choose a password with at least 8 characters.", token=csrf_token()), 400
        if not secrets.compare_digest(password, password_confirm):
            return render_template("setup.html", error="The passwords do not match.", token=csrf_token()), 400
        try:
            user_config_dir.mkdir(parents=True, exist_ok=True)
            env_path = user_config_dir / ".env"
            config = {
                "APP_USERNAME": username,
                "APP_PASSWORD": password,
                "FLASK_SECRET_KEY": secrets.token_urlsafe(48),
                "OPENAI_MODEL": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            }
            if api_key:
                config["OPENAI_API_KEY"] = api_key
            for name, value in config.items():
                set_key(env_path, name, value, quote_mode="always")
                os.environ[name] = value
            app.secret_key = os.environ["FLASK_SECRET_KEY"]
            session.clear()
            return redirect(url_for("login"))
        except OSError as exc:
            app.logger.exception("Could not save first-run settings")
            return render_template(
                "setup.html",
                error=f"Could not save settings in your Windows profile: {exc}",
                token=csrf_token(),
            ), 500
    return render_template("setup.html", token=csrf_token(), error=None)


@app.post("/logout")
def logout():
    dataset_id = session.get("dataset_id")
    session.clear()
    if dataset_id:
        datasets.pop(dataset_id, None)
    return redirect(url_for("login"))


@app.get("/")
def home():
    return render_template("index.html", token=csrf_token(), public_demo=public_demo_mode)


@app.get("/api/state")
def api_state():
    data = current_dataset()
    if not data:
        return jsonify({"dataset": None, "csrf_token": csrf_token()})
    return jsonify({"dataset": profile_payload(data["frame"])})


@app.post("/api/upload")
def api_upload():
    upload = request.files.get("file")
    if not upload:
        return response_error("Choose a dataset file to upload.")
    try:
        frame, filename = read_uploaded_file(upload)
        dataset_id = session.get("dataset_id") or secrets.token_urlsafe(18)
        datasets[dataset_id] = {"frame": frame, "filename": filename}
        session["dataset_id"] = dataset_id
        return jsonify({"dataset": profile_payload(frame)})
    except (ValueError, UnicodeError, pd.errors.ParserError, OSError) as exc:
        return response_error(exc)
    except Exception as exc:
        app.logger.exception("Dataset upload failed")
        return response_error(f"Dataset could not be loaded: {exc}")


@app.post("/api/data")
def api_data():
    try:
        frame = filtered_frame(require_dataset(), request.get_json(silent=True) or {})
        return jsonify(
            {
                "rows": [
                    {str(key): json_value(value) for key, value in row.items()}
                    for row in frame.head(250).to_dict(orient="records")
                ],
                "total": int(len(frame)),
            }
        )
    except Exception as exc:
        return response_error(exc)


def clean_column_names(frame):
    names = []
    seen = set()
    for original in frame.columns:
        name = re.sub(r"\s+", "_", str(original).strip().lower())
        name = re.sub(r"[^\w]", "", name, flags=re.UNICODE) or "column"
        candidate, suffix = name, 2
        while candidate in seen:
            candidate = f"{name}_{suffix}"
            suffix += 1
        names.append(candidate)
        seen.add(candidate)
    frame.columns = names


@app.post("/api/clean")
def api_clean():
    data = current_dataset()
    if not data:
        return response_error("Upload a dataset before cleaning it.")
    frame = data["frame"].copy()
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    column = payload.get("column")
    if column and column not in frame.columns:
        return response_error("The selected column does not exist.")
    if action in {"drop_column", "convert_type"} and not column:
        return response_error("Choose a column for this cleaning operation.")
    if action == "fill_missing" and payload.get("strategy", "mode") not in {"mean", "median", "mode", "custom"}:
        return response_error("Choose mean, median, mode, or a custom fill value.")
    try:
        if action == "strip_spaces":
            for name in frame.select_dtypes(include=["object", "string"]).columns:
                frame[name] = frame[name].astype("string").str.strip()
            frame.columns = [str(name).strip() for name in frame.columns]
        elif action == "remove_blank_rows":
            frame = frame.replace(r"^\s*$", pd.NA, regex=True).dropna(how="all")
        elif action == "remove_blank_columns":
            frame = frame.replace(r"^\s*$", pd.NA, regex=True).dropna(axis=1, how="all")
        elif action == "remove_duplicates":
            frame = frame.drop_duplicates()
        elif action == "safe_clean":
            for name in frame.select_dtypes(include=["object", "string"]).columns:
                frame[name] = frame[name].astype("string").str.strip()
            frame.columns = [str(name).strip() for name in frame.columns]
            frame = frame.replace(r"^\s*$", pd.NA, regex=True).dropna(how="all")
            frame = frame.dropna(axis=1, how="all").drop_duplicates()
            clean_column_names(frame)
        elif action == "fix_column_names":
            clean_column_names(frame)
        elif action == "drop_column":
            frame = frame.drop(columns=[column])
        elif action == "auto_convert_types":
            converted_columns = []
            skipped_columns = []
            for name in frame.columns:
                series = frame[name]
                detected = type_guess(series)
                if detected == "string" or is_identifier_column(name):
                    continue
                try:
                    if detected in {"integer", "float"}:
                        converted = pd.to_numeric(series, errors="coerce")
                        if converted.notna().sum() != series.notna().sum():
                            skipped_columns.append(str(name))
                            continue
                        frame[name] = converted
                    elif detected == "boolean":
                        mapping = {"true": True, "false": False, "yes": True, "no": False, "y": True, "n": False, "1": True, "0": False}
                        converted = series.astype("string").str.strip().str.lower().map(mapping)
                        if converted.notna().sum() != series.notna().sum():
                            skipped_columns.append(str(name))
                            continue
                        frame[name] = converted.astype("boolean")
                    elif detected == "date":
                        formats = date_format_counts(series)
                        dayfirst = formats["DD-MM-YYYY"] >= formats["MM-DD-YYYY"]
                        converted = pd.to_datetime(series, errors="coerce", format="mixed", dayfirst=dayfirst)
                        if converted.notna().sum() != series.notna().sum():
                            skipped_columns.append(str(name))
                            continue
                        frame[name] = converted
                    converted_columns.append(str(name))
                except (TypeError, ValueError, OverflowError):
                    skipped_columns.append(str(name))
            if not converted_columns and skipped_columns:
                return jsonify(error="No columns could be converted without losing values.", skipped=skipped_columns), 400
        elif action == "drop_missing_rows":
            frame = frame.dropna(subset=[column] if column else None)
        elif action == "drop_missing_columns":
            frame = frame.dropna(axis=1, how="any" if payload.get("only_incomplete") else "all")
        elif action == "fill_missing":
            target_columns = [column] if column else list(frame.columns)
            strategy = payload.get("strategy", "mode")
            for name in target_columns:
                series = frame[name]
                if strategy == "mean" and pd.api.types.is_numeric_dtype(series):
                    fill = series.mean()
                elif strategy == "median" and pd.api.types.is_numeric_dtype(series):
                    fill = series.median()
                elif strategy == "custom":
                    fill = payload.get("value", "")
                    if pd.api.types.is_numeric_dtype(series):
                        fill = pd.to_numeric(pd.Series([fill]), errors="raise").iloc[0]
                else:
                    modes = series.mode(dropna=True)
                    if modes.empty:
                        continue
                    fill = modes.iloc[0]
                frame[name] = series.fillna(fill)
        elif action == "convert_type":
            target = payload.get("target")
            series = frame[column]
            if target == "integer":
                converted = pd.to_numeric(series, errors="coerce")
                if (converted.dropna() % 1 != 0).any():
                    return response_error("This column contains fractional values and cannot be converted to integer without data loss.")
                frame[column] = converted.astype("Int64")
            elif target == "float":
                frame[column] = pd.to_numeric(series, errors="coerce")
            elif target == "date":
                text_values = series.astype("string")
                formats = date_format_counts(series)
                dayfirst = formats["DD-MM-YYYY"] >= formats["MM-DD-YYYY"]
                frame[column] = pd.to_datetime(text_values, errors="coerce", format="mixed", dayfirst=dayfirst)
            elif target == "boolean":
                mapping = {"true": True, "false": False, "yes": True, "no": False, "y": True, "n": False, "1": True, "0": False}
                frame[column] = series.astype("string").str.strip().str.lower().map(mapping).astype("boolean")
            elif target == "string":
                frame[column] = series.astype("string")
            else:
                return response_error("Choose a supported target type.")
        elif action == "remove_outliers":
            if not column or not pd.api.types.is_numeric_dtype(frame[column]):
                return response_error("Choose a numeric column for outlier removal.")
            values = frame[column]
            method = payload.get("method", "IQR")
            if method == "Z Score":
                std = values.std()
                keep = ((values - values.mean()).abs() / std <= 3) if std else values.notna()
            else:
                q1, q3 = values.quantile([0.25, 0.75])
                spread = q3 - q1
                keep = values.between(q1 - 1.5 * spread, q3 + 1.5 * spread) | values.isna()
            frame = frame.loc[keep]
        else:
            return response_error("Unknown cleaning operation.")
        data["frame"] = frame
        response = {"dataset": profile_payload(frame)}
        if action == "auto_convert_types":
            response["converted"] = converted_columns
            response["skipped"] = skipped_columns
        return jsonify(response)
    except (TypeError, ValueError, OverflowError) as exc:
        return response_error(f"Could not apply this cleaning operation: {exc}")


@app.post("/api/chart")
def api_chart():
    try:
        frame = filtered_frame(require_dataset(), request.get_json(silent=True) or {})
        payload = request.get_json(silent=True) or {}
        chart_type = payload.get("type", "bar")
        if chart_type not in {"bar", "pie", "donut", "histogram", "line", "scatter", "area", "box", "heatmap", "bubble", "treemap", "funnel", "waterfall"}:
            return response_error("Choose a supported chart type.")
        x, y = payload.get("x"), payload.get("y")
        color = payload.get("color", "#4f72f5")
        if chart_type == "heatmap":
            numeric = frame.select_dtypes(include="number")
            if numeric.empty:
                return response_error("A heatmap requires numeric columns.")
            fig = px.imshow(numeric.corr(), text_auto=".2f", aspect="auto", color_continuous_scale="Blues")
        elif chart_type in {"pie", "donut"}:
            if not x or x not in frame:
                return response_error("Choose a category column.")
            counts = frame[x].astype(str).value_counts().head(15).rename_axis(x).reset_index(name="count")
            fig = px.pie(counts, names=x, values="count", hole=0.55 if chart_type == "donut" else 0, color_discrete_sequence=[color, "#17b890", "#ffae42", "#8b5cf6", "#f15b6c"])
        elif chart_type == "histogram":
            fig = px.histogram(frame, x=x, nbins=30, color_discrete_sequence=[color])
        elif chart_type == "box":
            fig = px.box(frame, x=x if x in frame else None, y=y, color_discrete_sequence=[color])
        elif chart_type == "scatter":
            fig = px.scatter(frame, x=x, y=y, color_discrete_sequence=[color])
        elif chart_type == "bubble":
            size_column = payload.get("size")
            fig = px.scatter(frame, x=x, y=y, size=size_column if size_column in frame else None, color_discrete_sequence=[color])
        elif chart_type == "line":
            fig = px.line(frame, x=x, y=y, color_discrete_sequence=[color])
        elif chart_type == "area":
            fig = px.area(frame, x=x, y=y, color_discrete_sequence=[color])
        elif chart_type == "treemap":
            if not x or x not in frame or not y or y not in frame:
                return response_error("Treemaps require category and numeric value columns.")
            fig = px.treemap(frame, path=[x], values=y, color_discrete_sequence=[color])
        elif chart_type == "funnel":
            if not x or x not in frame or not y or y not in frame:
                return response_error("Funnels require stage and value columns.")
            fig = px.funnel(frame.groupby(x, dropna=False, as_index=False)[y].sum(), x=y, y=x, color_discrete_sequence=[color])
        elif chart_type == "waterfall":
            if not x or x not in frame or not y or y not in frame:
                return response_error("Waterfalls require category and numeric value columns.")
            grouped = frame.groupby(x, dropna=False, as_index=False)[y].sum().head(30)
            fig = go.Figure(go.Waterfall(x=grouped[x].astype(str), y=grouped[y], measure=["relative"] * len(grouped), increasing={"marker": {"color": color}}))
        else:
            if not x or x not in frame:
                return response_error("Choose a category or x-axis column.")
            if y and y in frame and pd.api.types.is_numeric_dtype(frame[y]):
                grouped = frame.groupby(x, dropna=False, as_index=False)[y].sum().head(100)
                fig = px.bar(grouped, x=x, y=y, color_discrete_sequence=[color])
            else:
                counts = frame[x].astype(str).value_counts().head(30).rename_axis(x).reset_index(name="count")
                fig = px.bar(counts, x=x, y="count", color_discrete_sequence=[color])
        fig.update_layout(
            template="plotly_white",
            margin={"l": 35, "r": 20, "t": 35, "b": 45},
            colorway=[color, "#17b890", "#ffae42", "#8b5cf6", "#f15b6c"],
        )
        return jsonify({"figure": json.loads(fig.to_json())})
    except Exception as exc:
        app.logger.exception("Chart generation failed")
        return response_error(f"Could not create this chart: {exc}")


def ai_profile(frame):
    report = dataset_report(frame)
    numeric_columns = [
        name for name in frame.select_dtypes(include="number").columns
        if not is_identifier_column(name)
    ][:5]
    safe_categories = {}
    business_breakdowns = {}
    business_category_name = re.compile(
        r"region|department|product|category|gender|segment|status|month|quarter|country|city|channel|source|team|industry",
        re.IGNORECASE,
    )
    sensitive_name = re.compile(
        r"email|phone|mobile|telephone|address|customer.?id|account.?id|ssn|social.?security|tax.?id|passport|birth|date.?of.?birth",
        re.IGNORECASE,
    )
    for name, values in report["categorical_top"].items():
        if not business_category_name.search(name) or sensitive_name.search(name) or is_identifier_column(name):
            continue
        safe_categories[name] = [
            item
            for item in values
            if not re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", item["value"])
        ]
        if name in frame.columns:
            category_breakdown = {}
            for metric in numeric_columns:
                grouped = frame.groupby(name, dropna=True)[metric].agg(["sum", "mean", "count"])
                grouped = grouped.sort_values("sum", ascending=False).head(10)
                category_breakdown[metric] = [
                    {
                        "category": str(category),
                        "sum": json_value(values["sum"]),
                        "mean": json_value(values["mean"]),
                        "count": int(values["count"]),
                    }
                    for category, values in grouped.iterrows()
                ]
            business_breakdowns[name] = category_breakdown

    time_series = {}
    for column in report["columns"]:
        name = column["name"]
        if column["guessed_type"] != "date":
            continue
        formats = date_format_counts(frame[name])
        dayfirst = formats["DD-MM-YYYY"] >= formats["MM-DD-YYYY"]
        dates = pd.to_datetime(frame[name], errors="coerce", format="mixed", dayfirst=dayfirst, utc=True)
        periods = dates.dt.tz_localize(None).dt.to_period("M").astype("string")
        series_by_metric = {}
        for metric in numeric_columns:
            values = pd.to_numeric(frame[metric], errors="coerce")
            grouped = pd.DataFrame({"period": periods, "value": values}).dropna().groupby("period")["value"].agg(["sum", "mean", "count"])
            if len(grouped) > 1:
                series_by_metric[metric] = [
                    {
                        "period": str(period),
                        "sum": json_value(values["sum"]),
                        "mean": json_value(values["mean"]),
                        "count": int(values["count"]),
                    }
                    for period, values in grouped.tail(36).iterrows()
                ]
        if series_by_metric:
            time_series[name] = series_by_metric

    return {
        "rows": report["rows"],
        "columns": report["columns"],
        "numeric_statistics": report["numeric_stats"],
        "outliers": report["outliers"],
        "common_categories": safe_categories,
        "business_breakdowns": business_breakdowns,
        "monthly_time_series": time_series,
        "missing_values": report["missing"],
        "duplicate_rows": report["duplicate_rows"],
    }


def local_business_story(frame):
    report = dataset_report(frame)
    sections = ["Business story | Four-stage analysis (generated locally)"]

    def add_section(title, points):
        sections.extend(["", title])
        sections.extend(f"- {point}" for point in points)

    def display(value):
        return f"{value:,.2f}" if isinstance(value, (int, float)) else str(value)

    descriptive = [
        f"The dataset contains {report['rows']:,} records and {report['columns_count']} columns."
    ]
    if report["missing"]:
        descriptive.append(
            f"There are {report['missing']:,} missing cells and {report['duplicate_rows']:,} duplicate rows to consider."
        )
    else:
        descriptive.append(f"There are no missing cells and {report['duplicate_rows']:,} duplicate rows.")

    numeric_stats = report["numeric_stats"]
    priority = re.compile(r"sales|revenue|profit|margin|cost|expense|quantity|amount|price|salary", re.IGNORECASE)
    metric_names = sorted(
        numeric_stats,
        key=lambda name: (not bool(priority.search(name)), name),
    )
    if numeric_stats:
        for name in metric_names[:5]:
            stats = numeric_stats[name]
            descriptive.append(
                f"{name}: average {display(stats['mean'])}, median {display(stats['median'])}, "
                f"range {display(stats['min'])} to {display(stats['max'])}."
            )
    else:
        descriptive.append("No numeric columns were found, so numeric performance measures are unavailable.")

    business_dimension = re.compile(
        r"region|department|product|category|gender|segment|status|month|quarter|country|city|channel|source|team|industry",
        re.IGNORECASE,
    )
    safe_column = re.compile(
        r"email|phone|mobile|telephone|address|customer.?id|account.?id|ssn|social.?security|tax.?id|passport|birth|date.?of.?birth",
        re.IGNORECASE,
    )
    dimensions = [
        name for name in frame.select_dtypes(exclude="number").columns
        if business_dimension.search(str(name))
        and not safe_column.search(str(name))
        and not is_identifier_column(name)
        and 1 < frame[name].nunique(dropna=True) <= 30
    ]
    popular_categories = []
    for name in dimensions:
        values = report["categorical_top"].get(str(name), [])
        if values and not re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", values[0]["value"]):
            popular_categories.append((str(name), values[0]))
    if popular_categories:
        for name, item in popular_categories[:5]:
            share = item["count"] / report["rows"] * 100 if report["rows"] else 0
            descriptive.append(
                f"{item['value']} is the most frequent value in {name} "
                f"({item['count']:,} records, {share:.1f}% of the dataset)."
            )
    add_section("1. Descriptive analysis - what happened?", descriptive)

    diagnostic = []
    regional_diagnostics = []
    regional_actions = []
    missing_columns = sorted(
        (column for column in report["columns"] if column["missing"]),
        key=lambda column: column["missing"],
        reverse=True,
    )
    if missing_columns:
        diagnostic.append(
            "Most missing values are in "
            + ", ".join(f"{column['name']} ({column['missing']:,})" for column in missing_columns[:4])
            + "."
        )
    else:
        diagnostic.append("No missing-value concentration was detected.")

    measure = next(
        (name for name in metric_names if re.search(r"profit|margin", name, re.IGNORECASE)),
        next((name for name in metric_names if re.search(r"sales|revenue|amount", name, re.IGNORECASE)), None),
    )
    if measure:
        profit_measure = bool(re.search(r"profit|margin", measure, re.IGNORECASE))
        numeric_values = pd.to_numeric(frame[measure], errors="coerce")
        negative_count = int((numeric_values < 0).sum()) if profit_measure else 0
        if profit_measure:
            if negative_count:
                diagnostic.append(
                    f"{negative_count:,} records have negative {measure} ({negative_count / max(report['rows'], 1) * 100:.1f}% of rows); investigate the affected transactions and segments before drawing conclusions."
                )
            else:
                diagnostic.append(f"No negative {measure} values were observed; this dataset does not show a recorded loss in that measure.")

        region_dimensions = [
            name for name in dimensions
            if re.search(r"region|country|state|territory|market", str(name), re.IGNORECASE)
        ]
        product_dimensions = [
            name for name in dimensions
            if re.search(r"product|category|item|service", str(name), re.IGNORECASE)
        ]
        sales_measure = next(
            (name for name in metric_names if re.search(r"sales|revenue", name, re.IGNORECASE)),
            None,
        )
        cost_measure = next(
            (name for name in metric_names if re.search(r"cost|expense", name, re.IGNORECASE)),
            None,
        )
        for region_column in region_dimensions:
            regional = pd.DataFrame({
                "region": frame[region_column],
                "profit": numeric_values,
            }).dropna(subset=["region", "profit"])
            regional_totals = regional.groupby("region")["profit"].agg(["sum", "mean", "count"])
            if len(regional_totals) < 2:
                continue
            score_column = "mean" if re.search(r"margin", measure, re.IGNORECASE) else "sum"
            loss_regions = regional_totals[regional_totals[score_column] < 0].sort_values(score_column)
            positive_regions = regional_totals[regional_totals[score_column] > 0].sort_values(score_column, ascending=False)
            best_region = positive_regions.index[0] if not positive_regions.empty else None
            if not loss_regions.empty:
                region_word = "region has" if len(loss_regions) == 1 else "regions have"
                regional_diagnostics.append(
                    f"{len(loss_regions)} {region_word} negative {measure}: "
                    + ", ".join(
                        f"{name} ({display(values[score_column])}, {int(values['count']):,} records)"
                        for name, values in loss_regions.iterrows()
                    )
                    + "."
                )
                if best_region is not None:
                    regional_diagnostics.append(
                        f"{best_region} is the strongest profitable comparison at "
                        f"{display(regional_totals.loc[best_region, score_column])} {measure}; "
                        "compare its product mix, pricing, costs, and operating conditions with the loss-making region(s)."
                    )

                for loss_region in loss_regions.index[:3]:
                    regional_actions.append(
                        f"Focus first on {loss_region}: review its transaction-level costs, discounts, returns, and expense allocations to locate the source of negative {measure}."
                    )
                    if product_dimensions:
                        product_column = product_dimensions[0]
                        in_region = pd.DataFrame({
                            "product": frame[product_column],
                            "profit": numeric_values,
                            "region": frame[region_column],
                        })
                        in_region = in_region.loc[in_region["region"] == loss_region].dropna(subset=["product", "profit"])
                        product_results = in_region.groupby("product")["profit"].agg(["sum", "mean", "count"])
                        product_score = "mean" if re.search(r"margin", measure, re.IGNORECASE) else "sum"
                        negative_products = product_results[product_results[product_score] < 0].sort_values(product_score)
                        if not negative_products.empty:
                            product_name, product_values = next(negative_products.iterrows())
                            regional_diagnostics.append(
                                f"Within {loss_region}, {product_name} is the weakest {product_column} at "
                                f"{display(product_values[product_score])} {measure} across {int(product_values['count']):,} records."
                            )
                            regional_actions.append(
                                f"Pilot a product-specific fix for {product_name} in {loss_region}: validate unit costs and discounting, then test a pricing, sourcing, or fulfillment change before scaling."
                            )
                    if best_region is not None:
                        regional_actions.append(
                            f"Use {best_region} as a benchmark for {loss_region}; compare like-for-like products and orders rather than assuming the same intervention will work in both regions."
                        )
                if sales_measure:
                    regional_actions.append(
                        f"Track {measure} alongside {sales_measure} and, where available, {cost_measure or 'operating costs'} in {loss_regions.index[0]}; higher sales alone may not improve profitability."
                    )
            else:
                weakest_region = regional_totals[score_column].idxmin()
                if best_region is not None and weakest_region != best_region:
                    regional_diagnostics.append(
                        f"No region has negative {measure}; {weakest_region} is the lowest at "
                        f"{display(regional_totals.loc[weakest_region, score_column])}, while "
                        f"{best_region} leads at {display(regional_totals.loc[best_region, score_column])}."
                    )
                    regional_actions.append(
                        f"Review {weakest_region}'s product mix, pricing, and costs against {best_region} to identify a measurable improvement opportunity; the data does not show a regional loss."
                    )
            break
        diagnostic.extend(regional_diagnostics)

        for dimension in dimensions[:8]:
            if dimension in region_dimensions:
                continue
            grouped = pd.DataFrame(
                {"group": frame[dimension], "value": numeric_values}
            ).dropna().groupby("group")["value"].agg(["mean", "sum", "count"])
            if len(grouped) < 2:
                continue
            statistic = "mean" if re.search(r"margin", measure, re.IGNORECASE) else "sum"
            high_group = grouped[statistic].idxmax()
            low_group = grouped[statistic].idxmin()
            if high_group == low_group:
                continue
            diagnostic.append(
                f"By {dimension}, {high_group} has the highest {measure} "
                f"({display(grouped.loc[high_group, statistic])}); {low_group} has the lowest "
                f"({display(grouped.loc[low_group, statistic])}). This is a comparison, not proof of cause."
            )
            break
    else:
        diagnostic.append("No suitable numeric business measure was identified for segment comparisons.")

    correlation_candidates = [
        name for name in metric_names
        if not is_identifier_column(name)
    ][:8]
    if len(correlation_candidates) > 1:
        correlations = frame[correlation_candidates].corr().abs()
        pairs = [
            (float(correlations.loc[first, second]), first, second)
            for index, first in enumerate(correlation_candidates)
            for second in correlation_candidates[index + 1:]
            if pd.notna(correlations.loc[first, second])
        ]
        if pairs:
            strength, first, second = max(pairs)
            if strength >= 0.5:
                diagnostic.append(
                    f"{first} and {second} move together in this dataset (correlation {strength:.2f}); this relationship does not establish causation."
                )
    add_section("2. Diagnostic analysis - what may explain the pattern?", diagnostic)

    predictive = []
    date_column = next(
        (column["name"] for column in report["columns"] if column["guessed_type"] == "date"),
        None,
    )
    forecast_metric = measure or next(
        (name for name in metric_names if not is_identifier_column(name)),
        None,
    )
    forecast_created = False
    if date_column and forecast_metric:
        formats = date_format_counts(frame[date_column])
        dates = pd.to_datetime(
            frame[date_column],
            errors="coerce",
            format="mixed",
            dayfirst=formats["DD-MM-YYYY"] >= formats["MM-DD-YYYY"],
        )
        monthly = pd.DataFrame({
            "period": dates.dt.to_period("M"),
            "value": pd.to_numeric(frame[forecast_metric], errors="coerce"),
        }).dropna().groupby("period")["value"].sum().sort_index()
        if len(monthly) >= 3:
            x = monthly.index.astype("int64").to_numpy(dtype=float)
            y = monthly.to_numpy(dtype=float)
            slope = float(((x - x.mean()) * (y - y.mean())).sum() / ((x - x.mean()) ** 2).sum())
            projected = [float(y[-1] + slope * step) for step in range(1, 7)]
            trend = "upward" if slope > 0 else "downward" if slope < 0 else "flat"
            predictive.append(
                f"Monthly {forecast_metric} has a {trend} linear trend across {len(monthly)} observed months. "
                f"A simple six-month extrapolation ranges from {display(projected[0])} to {display(projected[-1])} per month."
            )
            predictive.append("Treat this as a directional baseline, not a reliable forecast: it does not account for seasonality, business changes, or external factors.")
            forecast_created = True
        else:
            predictive.append(f"A forecast for {forecast_metric} was not generated: at least three observed months are needed for a basic trend.")
    if not forecast_created and not predictive:
        if not date_column and forecast_metric:
            predictive.append(f"A forecast for {forecast_metric} is unavailable because the dataset has no recognized date or period column.")
        elif date_column and not forecast_metric:
            predictive.append("A forecast is unavailable because no suitable numeric business measure was found.")
        else:
            predictive.append("A forecast is unavailable because the dataset needs both a recognized date column and a numeric business measure.")
    if not date_column:
        predictive.append("Add a consistently formatted date or period column to analyze trends and estimate future values.")
    add_section("3. Predictive analysis - what may happen next?", predictive)

    prescriptive = []
    if report["missing"] or report["duplicate_rows"]:
        prescriptive.append("Review missing cells and duplicate rows, then compare the key measures again to check whether the findings change.")
    prescriptive.extend(regional_actions)
    if measure and re.search(r"profit|margin", measure, re.IGNORECASE):
        if negative_count and not regional_actions:
            prescriptive.append(f"Prioritize reviewing the {negative_count:,} negative-{measure} records and their costs, pricing, and segment context; validate the underlying entries before taking action.")
        elif not negative_count and not regional_actions:
            prescriptive.append(f"Protect {measure} by monitoring it across products and regions; the current data does not justify assuming a loss problem.")
    elif measure:
        prescriptive.append(f"Track {measure} by the strongest and weakest groups above, and investigate operational differences before changing budgets or strategy.")
    else:
        prescriptive.append("Identify the business outcome to optimize (such as revenue, cost, or profit) and include it as a numeric field for actionable recommendations.")
    if not dimensions:
        prescriptive.append("Add a non-sensitive business segment such as product, region, or department to make comparisons more actionable.")
    prescriptive.append("Validate these patterns with domain owners and additional periods before making high-impact decisions.")
    add_section("4. Prescriptive analysis - what should we do?", prescriptive)

    return "\n".join(sections)


@app.post("/api/ai")
def api_ai():
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode", "story")
    try:
        frame = require_dataset()
    except ValueError as exc:
        return response_error(exc)
    if public_demo_mode:
        if mode == "story":
            return jsonify({"text": local_business_story(frame), "source": "local"})
        if mode == "insights":
            return jsonify({"text": "Insights & recommendations (local, rule-based; public demo)\n\n" + local_business_story(frame), "source": "local"})
        return response_error("Choose story or insights mode.", 400)
    if mode == "story" and not os.environ.get("OPENAI_API_KEY"):
        return jsonify({"text": local_business_story(frame), "source": "local"})
    if not os.environ.get("OPENAI_API_KEY"):
        if mode == "insights":
            return jsonify({
                "text": "Insights & recommendations (local, rule-based; no API key configured)\n\n"
                + local_business_story(frame),
                "source": "local",
            })
        return response_error("Choose story or insights mode.", 400)
    try:
        from openai import OpenAI

        context = json.dumps(ai_profile(frame), ensure_ascii=True, default=str)
        if mode == "insights":
            instruction = "Produce exactly 10 concise data-grounded insights, 10 prioritized recommendations, then short sections for business suggestions, risks, profit/loss, growth opportunities, seasonality, trends, and a six-month forecast only if an appropriate dated time series exists. Mark unsupported conclusions as unknown; do not invent facts or causal claims."
        else:
            instruction = "Write a professional, interview-ready business story in simple English. Explain the dataset, strongest supported findings, caveats, and concrete implications. Do not invent causal claims or facts."
        client = OpenAI()
        result = client.responses.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            input=f"{instruction}\n\nDataset aggregates (no raw records):\n{context}",
        )
        return jsonify({"text": result.output_text})
    except Exception as exc:
        app.logger.exception("AI analysis failed")
        return response_error(f"AI analysis failed: {exc}", 502)


@app.post("/api/export/<kind>")
def api_export(kind):
    frame = require_dataset()
    filename = Path(current_dataset()["filename"]).stem or "dataset"
    try:
        output = io.BytesIO()
        if kind == "csv":
            output.write(frame.to_csv(index=False).encode("utf-8-sig"))
            mimetype, extension = "text/csv", "csv"
        elif kind == "excel":
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                frame.to_excel(writer, index=False, sheet_name="Cleaned data")
                pd.DataFrame(dataset_report(frame)["columns"]).to_excel(writer, index=False, sheet_name="Missing data")
            mimetype, extension = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
        elif kind == "pdf":
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import letter
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
            from reportlab.lib.styles import getSampleStyleSheet

            report = dataset_report(frame)
            doc = SimpleDocTemplate(output, pagesize=letter)
            styles = getSampleStyleSheet()
            table = Table([["Metric", "Value"], ["Rows", str(report["rows"])], ["Columns", str(report["columns_count"])], ["Missing values", str(report["missing"])], ["Duplicate rows", str(report["duplicate_rows"])]] + [["Numeric column", "Mean / Median / Std"]] + [[name, f"{stats['mean']:.3g} / {stats['median']:.3g} / {stats['std']:.3g}" if stats["std"] is not None else f"{stats['mean']:.3g} / {stats['median']:.3g} / n/a"] for name, stats in report["numeric_stats"].items()])
            table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#243451")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f6fb")])]))
            doc.build([Paragraph(f"{filename} - Dataset report", styles["Title"]), Spacer(1, 14), table])
            mimetype, extension = "application/pdf", "pdf"
        elif kind == "pptx":
            from pptx import Presentation
            from pptx.util import Inches, Pt

            report = dataset_report(frame)
            presentation = Presentation()
            slide = presentation.slides.add_slide(presentation.slide_layouts[0])
            slide.shapes.title.text = f"{filename} - Data story"
            slide.placeholders[1].text = f"{report['rows']:,} rows | {report['columns_count']} columns | {report['missing']:,} missing values | {report['duplicate_rows']:,} duplicate rows"
            slide = presentation.slides.add_slide(presentation.slide_layouts[5])
            slide.shapes.title.text = "Numeric summary"
            lines = [f"{name}: mean {stats['mean']:.3g}, median {stats['median']:.3g}, range {stats['min']:.3g} to {stats['max']:.3g}" for name, stats in report["numeric_stats"].items()]
            box = slide.shapes.add_textbox(Inches(0.8), Inches(1.5), Inches(8.5), Inches(5.2))
            box.text_frame.text = "\n".join(lines[:18]) or "No numeric columns available."
            box.text_frame.paragraphs[0].font.size = Pt(18)
            presentation.save(output)
            mimetype, extension = "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"
        else:
            return response_error("Unsupported export format.")
        output.seek(0)
        return send_file(output, mimetype=mimetype, as_attachment=True, download_name=f"{filename}-report.{extension}")
    except Exception as exc:
        app.logger.exception("Export failed")
        return response_error(f"Export failed: {exc}", 500)


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify(error="The upload exceeds the 20 MB file limit."), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
