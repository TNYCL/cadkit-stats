#!/usr/bin/env python3
"""Collect public download and repository statistics for cadkit.

Standard library only (Python 3.12+). Sources: crates.io, pypistats.org,
api.npmjs.org and the GitHub REST API. Output: one raw snapshot per UTC day in
data/raw/, merged daily history in data/series/*.csv and data/summary.json
(contract: schema/summary.schema.json). See README.md.

Exit codes: 0 = at least one source succeeded and a valid summary was written;
1 = every source failed (nothing written); 2 = a summary could not be built or
failed validation (raw snapshot and series are still written).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 1

CRATES_API = "https://crates.io/api/v1"
PYPISTATS_API = "https://pypistats.org/api"
NPM_API = "https://api.npmjs.org"
GITHUB_API = "https://api.github.com"

# Order matters: it is the order of the "sources" object in the outputs.
SOURCES = ("crates_io", "pypi", "npm", "github", "github_traffic")
PYPI_OS = ("Linux", "Darwin", "Windows", "other", "null")

# crates.io returns per-day downloads for the last 90 days. Days inside the
# window that are missing from the response had zero downloads; the two oldest
# days are left alone so a boundary day is never overwritten with a false zero.
CRATES_ZERO_FILL_DAYS = 88
# pypistats.org keeps about 180 days of history.
PYPI_ZERO_FILL_DAYS = 178
# api.npmjs.org limits a range query to 18 months.
NPM_RANGE_DAYS = 540
MAX_PAGES = 50

Getter = Callable[..., Any]


class FetchError(Exception):
    """A request failed after retries. The message never contains headers."""


# --------------------------------------------------------------------------
# HTTP


class HttpClient:
    """Polite JSON GET client: per-host spacing, timeouts, retries with backoff."""

    RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        user_agent: str,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 2.0,
        min_interval: float = 1.0,
        max_retry_after: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.min_interval = min_interval
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self._clock = clock
        self._urlopen = urlopen
        self._last: dict[str, float] = {}

    def _throttle(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.min_interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = self._clock()

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        host = urllib.parse.urlsplit(url).hostname or ""
        merged = {"User-Agent": self.user_agent, "Accept": "application/json"}
        merged.update(headers or {})
        attempt = 0
        while True:
            self._throttle(host)
            request = urllib.request.Request(url, headers=merged, method="GET")
            delay = self.backoff * (2**attempt)
            try:
                with self._urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                return json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as err:
                status = err.code
                retry_after = err.headers.get("Retry-After") if err.headers else None
                err.close()
                if status not in self.RETRY_STATUS or attempt >= self.retries:
                    raise FetchError(f"HTTP {status} for {url}") from None
                if retry_after and retry_after.strip().isdigit():
                    delay = min(float(retry_after.strip()), self.max_retry_after)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                if attempt >= self.retries:
                    reason = getattr(err, "reason", None) or type(err).__name__
                    raise FetchError(f"network error for {url}: {reason}") from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                if attempt >= self.retries:
                    raise FetchError(f"invalid JSON from {url}") from None
            attempt += 1
            self._sleep(delay)


# --------------------------------------------------------------------------
# Small helpers


def iso_now(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def day(value: dt.date) -> str:
    return value.isoformat()


def daterange(start: dt.date, end: dt.date) -> list[str]:
    days = (end - start).days
    return [day(start + dt.timedelta(days=i)) for i in range(days + 1)] if days >= 0 else []


def as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"expected an integer, got {type(value).__name__}")
    return int(value)


def valid_date(value: Any) -> str:
    text = str(value)[:10]
    dt.date.fromisoformat(text)
    return text


def sorted_daily(daily: dict[str, int]) -> dict[str, int]:
    return {k: daily[k] for k in sorted(daily)}


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def write_text(path: Path, text: str) -> None:
    """Atomic write; leaves the file untouched when the content is identical."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("github_repo", "user_agent", "crates", "pypi_package", "npm_package"):
        if key not in cfg:
            raise ValueError(f"config.json: missing key {key!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", cfg["github_repo"]):
        raise ValueError("config.json: github_repo must be 'owner/name'")
    return cfg


# --------------------------------------------------------------------------
# Fetchers. Each returns a JSON-serialisable section with extracted fields only
# (no user records, avatars or response headers).


def fetch_crates(get: Getter, cfg: dict[str, Any], today: dt.date) -> dict[str, Any]:
    own = set(cfg["crates"])
    crates: list[dict[str, Any]] = []
    daily: dict[str, dict[str, int]] = {}
    external: set[str] = set()
    for name in cfg["crates"]:
        quoted = urllib.parse.quote(name, safe="")
        info = get(f"{CRATES_API}/crates/{quoted}")["crate"]
        downloads = get(f"{CRATES_API}/crates/{quoted}/downloads")
        per_day: dict[str, int] = {}
        rows = list(downloads.get("version_downloads") or [])
        rows += list((downloads.get("meta") or {}).get("extra_downloads") or [])
        for row in rows:
            date = valid_date(row["date"])
            per_day[date] = per_day.get(date, 0) + as_int(row["downloads"])
        dependents = fetch_reverse_dependents(get, quoted) - own
        external |= dependents
        crates.append(
            {
                "name": name,
                "downloads": as_int(info["downloads"]),
                "recent_90d": as_int(info.get("recent_downloads") or 0),
                "max_version": str(info["max_version"]),
                "reverse_dependencies": len(dependents),
                "reverse_dependents": sorted(dependents),
            }
        )
        daily[name] = sorted_daily(per_day)
    start = today - dt.timedelta(days=CRATES_ZERO_FILL_DAYS)
    return {
        "crates": crates,
        "external_reverse_dependents": sorted(external),
        "daily": daily,
        "zero_fill": {"start": day(start), "end": day(today)},
    }


def fetch_reverse_dependents(get: Getter, quoted: str) -> set[str]:
    names: set[str] = set()
    seen = 0
    for page in range(1, MAX_PAGES + 1):
        data = get(f"{CRATES_API}/crates/{quoted}/reverse_dependencies?page={page}&per_page=100")
        deps = data.get("dependencies") or []
        versions = {v["id"]: v["crate"] for v in data.get("versions") or []}
        for dep in deps:
            crate = versions.get(dep.get("version_id"))
            if crate:
                names.add(str(crate))
        seen += len(deps)
        total = as_int((data.get("meta") or {}).get("total") or 0)
        if not deps or seen >= total:
            break
    return names


def fetch_pypi(get: Getter, cfg: dict[str, Any], today: dt.date) -> dict[str, Any]:
    pkg = urllib.parse.quote(cfg["pypi_package"], safe="")
    recent = get(f"{PYPISTATS_API}/packages/{pkg}/recent")["data"]
    overall = get(f"{PYPISTATS_API}/packages/{pkg}/overall")["data"]
    system = get(f"{PYPISTATS_API}/packages/{pkg}/system?mirrors=false")["data"]
    mirrors: dict[str, dict[str, int]] = {"without_mirrors": {}, "with_mirrors": {}}
    for row in overall:
        cat = row.get("category")
        if cat in mirrors:
            date = valid_date(row["date"])
            mirrors[cat][date] = mirrors[cat].get(date, 0) + as_int(row["downloads"])
    os_daily: dict[str, dict[str, int]] = {name: {} for name in PYPI_OS}
    for row in system:
        cat = row.get("category")
        cat = "null" if cat is None or cat == "null" else str(cat)
        if cat not in os_daily:
            cat = "other"
        date = valid_date(row["date"])
        os_daily[cat][date] = os_daily[cat].get(date, 0) + as_int(row["downloads"])
    dates = [d for series in list(mirrors.values()) + list(os_daily.values()) for d in series]
    latest = max(dates) if dates else None
    start = today - dt.timedelta(days=PYPI_ZERO_FILL_DAYS)
    return {
        "package": cfg["pypi_package"],
        "recent": {k: as_int(recent[k]) for k in ("last_day", "last_week", "last_month")},
        "daily": {k: sorted_daily(v) for k, v in mirrors.items()},
        "os_daily_without_mirrors": {k: sorted_daily(v) for k, v in os_daily.items()},
        # Zero-fill only up to the newest day pypistats has computed.
        "zero_fill": {"start": day(start), "end": latest} if latest else None,
    }


def fetch_npm(get: Getter, cfg: dict[str, Any], today: dt.date) -> dict[str, Any]:
    pkg = urllib.parse.quote(cfg["npm_package"], safe="@/")
    week = get(f"{NPM_API}/downloads/point/last-week/{pkg}")
    month = get(f"{NPM_API}/downloads/point/last-month/{pkg}")
    start = today - dt.timedelta(days=NPM_RANGE_DAYS)
    rng = get(f"{NPM_API}/downloads/range/{day(start)}:{day(today)}/{pkg}")
    daily: dict[str, int] = {}
    for row in rng.get("downloads") or []:
        daily[valid_date(row["day"])] = as_int(row["downloads"])
    return {
        "package": cfg["npm_package"],
        "last_week": as_int(week["downloads"]),
        "last_month": as_int(month["downloads"]),
        "daily": sorted_daily(daily),
    }


def github_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_github(get: Getter, cfg: dict[str, Any], token: str | None) -> dict[str, Any]:
    repo = cfg["github_repo"]
    headers = github_headers(token)
    info = get(f"{GITHUB_API}/repos/{repo}", headers=headers)
    assets: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        releases = get(f"{GITHUB_API}/repos/{repo}/releases?per_page=100&page={page}", headers=headers)
        for release in releases:
            if release.get("draft"):
                continue
            for asset in sorted(release.get("assets") or [], key=lambda a: str(a["name"])):
                assets.append(
                    {
                        "tag": str(release["tag_name"]),
                        "asset": str(asset["name"]),
                        "downloads": as_int(asset["download_count"]),
                    }
                )
        if len(releases) < 100:
            break
    return {
        "repo": repo,
        "stars": as_int(info["stargazers_count"]),
        "forks": as_int(info["forks_count"]),
        # GitHub counts open pull requests as open issues.
        "open_issues": as_int(info["open_issues_count"]),
        # watchers_count is a legacy alias of stars; subscribers are real watchers.
        "watchers": as_int(info["subscribers_count"]),
        "release_assets": assets,
    }


def fetch_github_traffic(get: Getter, cfg: dict[str, Any], token: str) -> dict[str, Any]:
    repo = cfg["github_repo"]
    headers = github_headers(token)
    views = get(f"{GITHUB_API}/repos/{repo}/traffic/views", headers=headers)
    clones = get(f"{GITHUB_API}/repos/{repo}/traffic/clones", headers=headers)
    referrers = get(f"{GITHUB_API}/repos/{repo}/traffic/popular/referrers", headers=headers)
    paths = get(f"{GITHUB_API}/repos/{repo}/traffic/popular/paths", headers=headers)

    def per_day(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
        out = {valid_date(r["timestamp"]): [as_int(r["count"]), as_int(r["uniques"])] for r in rows}
        return {k: out[k] for k in sorted(out)}

    return {
        "views_14d": {"count": as_int(views["count"]), "uniques": as_int(views["uniques"])},
        "clones_14d": {"count": as_int(clones["count"]), "uniques": as_int(clones["uniques"])},
        "views_daily": per_day(views.get("views") or []),
        "clones_daily": per_day(clones.get("clones") or []),
        "popular_referrers": [
            {"referrer": str(r["referrer"]), "count": as_int(r["count"]), "uniques": as_int(r["uniques"])}
            for r in referrers
        ],
        "popular_paths": [
            {"path": str(p["path"]), "count": as_int(p["count"]), "uniques": as_int(p["uniques"])}
            for p in paths
        ],
    }


# --------------------------------------------------------------------------
# Series CSV storage: "date,<col>,<col>..." ascending; empty cell = unknown.

Rows = dict[str, dict[str, int | None]]


def read_series(path: Path) -> tuple[list[str], Rows]:
    if not path.exists():
        return [], {}
    reader = csv.reader(io.StringIO(path.read_text(encoding="utf-8")))
    header = next(reader, None)
    if not header or header[0] != "date":
        raise ValueError(f"{path}: first column must be 'date'")
    columns = header[1:]
    rows: Rows = {}
    for record in reader:
        if not record:
            continue
        date = valid_date(record[0])
        values = record[1:] + [""] * (len(columns) - len(record) + 1)
        rows[date] = {c: (int(v) if v != "" else None) for c, v in zip(columns, values)}
    return columns, rows


def write_series(path: Path, columns: list[str], rows: Rows) -> None:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["date", *columns])
    for date in sorted(rows):
        writer.writerow([date, *("" if rows[date].get(c) is None else rows[date][c] for c in columns)])
    write_text(path, out.getvalue())


def merge_rows(old: Rows, new: Rows) -> Rows:
    """Keep every old day; for overlapping days the newest non-empty values win."""
    merged: Rows = {d: dict(v) for d, v in old.items()}
    for date, values in new.items():
        target = merged.setdefault(date, {})
        for col, value in values.items():
            if value is not None:
                target[col] = value
    return merged


def merge_series(path: Path, columns: list[str], new: Rows) -> Rows:
    old_columns, old = read_series(path)
    merged = merge_rows(old, new)
    order = list(columns) + [c for c in old_columns if c not in columns]
    write_series(path, order, merged)
    return merged


def zero_filled(daily: dict[str, int], start: str, end: str) -> dict[str, int]:
    out = dict(daily)
    for date in daterange(dt.date.fromisoformat(start), dt.date.fromisoformat(end)):
        out.setdefault(date, 0)
    return out


def update_series(series_dir: Path, name: str, section: dict[str, Any]) -> None:
    """Merge one freshly fetched section into its CSV files."""
    if name == "crates_io":
        names = [c["name"] for c in section["crates"]]
        fill = section["zero_fill"]
        new: Rows = {}
        for crate in names:
            for date, n in zero_filled(section["daily"].get(crate, {}), fill["start"], fill["end"]).items():
                new.setdefault(date, {})[crate] = n
        path = series_dir / "crates_io.csv"
        old_columns, old = read_series(path)
        merged = merge_rows(old, new)
        crate_cols = names + [c for c in old_columns if c not in names and c != "total"]
        for values in merged.values():
            values["total"] = sum(values.get(c) or 0 for c in crate_cols)
        write_series(path, ["total", *crate_cols], merged)
    elif name == "pypi":
        fill = section["zero_fill"]
        new = {}
        for col in ("without_mirrors", "with_mirrors"):
            daily = section["daily"][col]
            if fill:
                daily = zero_filled(daily, fill["start"], fill["end"])
            for date, n in daily.items():
                new.setdefault(date, {})[col] = n
        merge_series(series_dir / "pypi.csv", ["without_mirrors", "with_mirrors"], new)
        new = {}
        for col in PYPI_OS:
            daily = section["os_daily_without_mirrors"][col]
            if fill:
                daily = zero_filled(daily, fill["start"], fill["end"])
            for date, n in daily.items():
                new.setdefault(date, {})[col] = n
        merge_series(series_dir / "pypi_system.csv", list(PYPI_OS), new)
    elif name == "npm":
        new = {d: {"downloads": n} for d, n in section["daily"].items()}
        merge_series(series_dir / "npm.csv", ["downloads"], new)
    elif name == "github_traffic":
        for key, file_name, col in (
            ("views_daily", "github_views.csv", "views"),
            ("clones_daily", "github_clones.csv", "clones"),
        ):
            new = {d: {col: v[0], "uniques": v[1]} for d, v in section[key].items()}
            merge_series(series_dir / file_name, [col, "uniques"], new)


def update_repo_series(series_dir: Path, date: str, section: dict[str, Any]) -> None:
    """Point-in-time repository counters, one row per collection day."""
    row = {
        "stars": section["stars"],
        "forks": section["forks"],
        "open_issues": section["open_issues"],
        "watchers": section["watchers"],
        "release_asset_downloads": sum(a["downloads"] for a in section["release_assets"]),
    }
    merge_series(series_dir / "github_repo.csv", list(row), {date: row})


# --------------------------------------------------------------------------
# Raw snapshots


def read_raw(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def latest_known(raw_dir: Path, name: str) -> tuple[dict[str, Any], str] | None:
    """Newest successful section for a source across all raw snapshots."""
    for path in sorted(raw_dir.glob("????-??-??.json"), reverse=True):
        raw = read_raw(path) or {}
        status = (raw.get("sources") or {}).get(name) or {}
        section = (raw.get("data") or {}).get(name)
        if status.get("ok") and section is not None:
            return section, status["fetched_at"]
    return None


# --------------------------------------------------------------------------
# Summary


NOTES = [
    "Download counts include CI systems, mirrors, caches and bots; they are not a count of users.",
    "crates.io totals are all-time; crates.io per-day downloads cover the last 90 days per request and the collector keeps older days.",
    "crates.io updates per-day counts and all-time totals on different schedules, so the daily series can briefly sum to slightly more than the total.",
    "PyPI numbers come from pypistats.org. 'without mirrors' excludes known mirroring clients; OS 'null' means the installer did not report a platform.",
    "npm counts are registry tarball downloads for cadkit-wasm as reported by api.npmjs.org.",
    "GitHub traffic is a rolling 14-day window; the daily view and clone series contain only days the collector captured.",
    "GitHub 'watchers' are repository subscribers, not stars; 'open_issues' includes open pull requests.",
    "The most recent day of each daily series may be incomplete.",
]


def series_from(rows: Rows, cols: list[str]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for date in sorted(rows):
        values = [rows[date].get(c) for c in cols]
        if any(v is None for v in values):
            continue
        if not out and all(v == 0 for v in values):
            continue  # drop leading days before the first download
        out.append([date, *values])
    return out


def build_summary(
    data_dir: Path,
    sections: dict[str, dict[str, Any] | None],
    statuses: dict[str, dict[str, Any]],
    now: dt.datetime,
) -> dict[str, Any]:
    series_dir = data_dir / "series"
    crates = sections["crates_io"]
    pypi = sections["pypi"]
    npm = sections["npm"]
    github = sections["github"]
    traffic = sections["github_traffic"]
    assert crates and pypi and npm and github

    _, crates_rows = read_series(series_dir / "crates_io.csv")
    _, pypi_rows = read_series(series_dir / "pypi.csv")
    _, os_rows = read_series(series_dir / "pypi_system.csv")
    _, npm_rows = read_series(series_dir / "npm.csv")
    _, views_rows = read_series(series_dir / "github_views.csv")
    _, clones_rows = read_series(series_dir / "github_clones.csv")

    def column_sum(rows: Rows, col: str) -> int:
        return sum(v.get(col) or 0 for v in rows.values())

    release_assets = [dict(a) for a in github["release_assets"]]
    notes = list(NOTES)
    for name in SOURCES:
        st = statuses[name]
        if st.get("skipped"):
            when = st.get("last_success_at")
            notes.append(
                f"{name}: not collected in this run ({st['error']}); "
                + (f"values are from {when}." if when else "no earlier capture exists.")
            )
        elif not st["ok"]:
            notes.append(f"{name}: failed in this run; values are from the last successful collection at {st['last_success_at']}.")

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": iso_now(now),
        "totals": {
            "crates_io": sum(c["downloads"] for c in crates["crates"]),
            "pypi_without_mirrors": column_sum(pypi_rows, "without_mirrors"),
            "pypi_with_mirrors": column_sum(pypi_rows, "with_mirrors"),
            "npm": column_sum(npm_rows, "downloads"),
            "github_release_assets": sum(a["downloads"] for a in release_assets),
            "crates_io_reverse_dependencies": len(crates.get("external_reverse_dependents") or []),
        },
        "crates": [
            {
                "name": c["name"],
                "downloads": c["downloads"],
                "recent_90d": c["recent_90d"],
                "max_version": c["max_version"],
                "reverse_dependencies": c["reverse_dependencies"],
            }
            for c in crates["crates"]
        ],
        "pypi": {
            "package": pypi["package"],
            "last_day": pypi["recent"]["last_day"],
            "last_week": pypi["recent"]["last_week"],
            "last_month": pypi["recent"]["last_month"],
            "os_without_mirrors": {k: column_sum(os_rows, k) for k in PYPI_OS},
        },
        "npm": {"package": npm["package"], "last_week": npm["last_week"], "last_month": npm["last_month"]},
        "github": {
            "repo": github["repo"],
            "stars": github["stars"],
            "forks": github["forks"],
            "open_issues": github["open_issues"],
            "watchers": github["watchers"],
            "release_assets": release_assets,
            "traffic_14d": (
                {
                    "views": traffic["views_14d"]["count"],
                    "unique_views": traffic["views_14d"]["uniques"],
                    "clones": traffic["clones_14d"]["count"],
                    "unique_clones": traffic["clones_14d"]["uniques"],
                }
                if traffic
                else None
            ),
            "popular_referrers_14d": traffic["popular_referrers"] if traffic else None,
            "popular_paths_14d": traffic["popular_paths"] if traffic else None,
        },
        "series": {
            "crates_io": series_from(crates_rows, ["total"]),
            "pypi_without_mirrors": series_from(pypi_rows, ["without_mirrors"]),
            "npm": series_from(npm_rows, ["downloads"]),
            "github_views": series_from(views_rows, ["views", "uniques"]),
            "github_clones": series_from(clones_rows, ["clones", "uniques"]),
        },
        "sources": {name: statuses[name] for name in SOURCES},
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Minimal JSON Schema (draft 2020-12) validator for the subset the schema uses:
# $ref (local), type, enum, const, properties, required, additionalProperties,
# items, prefixItems, minItems, maxItems, minimum, pattern, minLength.


def _type_ok(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise ValueError(f"unsupported type {name!r}")


def validate(value: Any, schema: Any, root: Any = None, path: str = "$") -> list[str]:
    root = schema if root is None else root
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: not allowed"]
    errors: list[str] = []
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"unsupported $ref {ref!r}")
        target = root
        for part in ref[2:].split("/"):
            target = target[part]
        errors += validate(value, target, root, path)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, t) for t in types):
            return errors + [f"{path}: expected {'|'.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not in enum")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than minLength")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required {key!r}")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                errors += validate(item, props[key], root, f"{path}.{key}")
            else:
                errors += validate(item, extra, root, f"{path}.{key}")
    if isinstance(value, list):
        prefix = schema.get("prefixItems", [])
        for i, item in enumerate(value):
            sub = prefix[i] if i < len(prefix) else schema.get("items", True)
            errors += validate(item, sub, root, f"{path}[{i}]")
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
    return errors


def check_summary(summary: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Schema validation plus cross-field consistency checks."""
    errors = validate(summary, schema)
    if errors:
        return errors
    totals = summary["totals"]
    if totals["crates_io"] != sum(c["downloads"] for c in summary["crates"]):
        errors.append("totals.crates_io != sum(crates[].downloads)")
    if totals["github_release_assets"] != sum(a["downloads"] for a in summary["github"]["release_assets"]):
        errors.append("totals.github_release_assets != sum(github.release_assets[].downloads)")
    for name, points in summary["series"].items():
        dates = [p[0] for p in points]
        if dates != sorted(set(dates)):
            errors.append(f"series.{name}: dates are not strictly ascending")
    return errors


# --------------------------------------------------------------------------
# Orchestration


def run(
    cfg: dict[str, Any],
    data_dir: Path,
    get: Getter,
    env: dict[str, str],
    now: dt.datetime,
    schema: dict[str, Any],
    log: Callable[[str], None] = print,
) -> int:
    today = now.astimezone(dt.timezone.utc).date()
    stamp = iso_now(now)
    traffic_token = env.get("TRAFFIC_TOKEN") or None
    github_token = env.get("GITHUB_TOKEN") or None
    secrets = [t for t in (traffic_token, github_token) if t]

    def scrub(message: str) -> str:
        for secret in secrets:
            message = message.replace(secret, "[redacted]")
        return message

    fetchers: dict[str, Callable[[], dict[str, Any]]] = {
        "crates_io": lambda: fetch_crates(get, cfg, today),
        "pypi": lambda: fetch_pypi(get, cfg, today),
        "npm": lambda: fetch_npm(get, cfg, today),
        "github": lambda: fetch_github(get, cfg, github_token),
        "github_traffic": lambda: fetch_github_traffic(get, cfg, traffic_token or ""),
    }
    fresh: dict[str, dict[str, Any]] = {}
    statuses: dict[str, dict[str, Any]] = {}
    for name in SOURCES:
        if name == "github_traffic" and not traffic_token:
            statuses[name] = {"ok": False, "fetched_at": stamp, "error": "skipped: TRAFFIC_TOKEN is not set", "skipped": True}
            continue
        try:
            fresh[name] = fetchers[name]()
            statuses[name] = {"ok": True, "fetched_at": stamp, "error": None}
        except Exception as err:  # noqa: BLE001 - any failure of one source must not stop the others
            message = str(err) if isinstance(err, FetchError) else f"{type(err).__name__}: {err}"
            statuses[name] = {"ok": False, "fetched_at": stamp, "error": scrub(message)[:500]}

    for name in SOURCES:
        st = statuses[name]
        line = f"{name}: " + ("ok" if st["ok"] else st["error"])
        log(line)
        if not st["ok"] and not st.get("skipped") and env.get("GITHUB_ACTIONS") == "true":
            log(f"::warning::{line}")

    if not fresh:
        log("every source failed; nothing was written")
        return 1

    raw_dir = data_dir / "raw"
    series_dir = data_dir / "series"
    raw_path = raw_dir / f"{day(today)}.json"
    previous = read_raw(raw_path) or {}
    raw_sources: dict[str, Any] = {}
    raw_data: dict[str, Any] = {}
    for name in SOURCES:
        old_status = (previous.get("sources") or {}).get(name) or {}
        old_data = (previous.get("data") or {}).get(name)
        if name in fresh:
            raw_sources[name], raw_data[name] = statuses[name], fresh[name]
        elif old_status.get("ok") and old_data is not None:
            # An earlier run today succeeded: keep its data (idempotent re-runs).
            raw_sources[name], raw_data[name] = old_status, old_data
        else:
            raw_sources[name] = statuses[name]
    write_text(raw_path, dump_json({"schema": SCHEMA_VERSION, "date": day(today), "sources": raw_sources, "data": raw_data}))

    for name, section in fresh.items():
        update_series(series_dir, name, section)
    if "github" in fresh:
        update_repo_series(series_dir, day(today), fresh["github"])

    sections: dict[str, dict[str, Any] | None] = {}
    missing = []
    for name in SOURCES:
        known = latest_known(raw_dir, name)
        sections[name] = known[0] if known else None
        statuses[name]["last_success_at"] = known[1] if known else None
        if known is None and name != "github_traffic":
            missing.append(name)
    if missing:
        log(f"no data has ever been collected for: {', '.join(missing)}; summary.json not written")
        return 2

    summary = build_summary(data_dir, sections, statuses, now)
    errors = check_summary(summary, schema)
    if errors:
        for error in errors[:20]:
            log(f"summary invalid: {error}")
        return 2
    write_text(data_dir / "summary.json", dump_json(summary))
    t = summary["totals"]
    log(
        "totals: crates.io {crates_io}, PyPI without mirrors {pypi_without_mirrors} "
        "(with mirrors {pypi_with_mirrors}), npm {npm}, GitHub release assets {github_release_assets}".format(**t)
    )
    return 0


def main() -> int:
    cfg = load_config(ROOT / "config.json")
    schema = json.loads((ROOT / "schema" / "summary.schema.json").read_text(encoding="utf-8"))
    client = HttpClient(cfg["user_agent"])
    env = {k: os.environ[k] for k in ("TRAFFIC_TOKEN", "GITHUB_TOKEN", "GITHUB_ACTIONS") if k in os.environ}
    return run(cfg, ROOT / "data", client.get_json, env, dt.datetime.now(dt.timezone.utc), schema)


if __name__ == "__main__":
    sys.exit(main())
