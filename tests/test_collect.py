"""Offline tests for collect.py. Run: python -m unittest discover -s tests -t ."""

from __future__ import annotations

import copy
import datetime as dt
import email.message
import io
import json
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path

import collect

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
ROOT = HERE.parent
SCHEMA = json.loads((ROOT / "schema" / "summary.schema.json").read_text(encoding="utf-8"))
CONFIG = collect.load_config(ROOT / "config.json")
NOW = dt.datetime(2026, 11, 16, 3, 17, tzinfo=dt.timezone.utc)
SECRET = "fixture-token-FIXTURESECRET0123456789"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeApi:
    """Routes URLs to synthetic payloads shaped like the real APIs."""

    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()
        self.calls: list[tuple[str, dict]] = []
        self.payloads = {
            "crate": fixture("crates_crate.json"),
            "downloads": fixture("crates_downloads.json"),
            "revdeps": fixture("crates_reverse_dependencies.json"),
            "pypi_recent": fixture("pypi_recent.json"),
            "pypi_overall": fixture("pypi_overall.json"),
            "pypi_system": fixture("pypi_system.json"),
            "npm_week": fixture("npm_point_last_week.json"),
            "npm_month": fixture("npm_point_last_month.json"),
            "npm_range": fixture("npm_range.json"),
            "gh_repo": fixture("github_repo.json"),
            "gh_releases": fixture("github_releases.json"),
            "views": fixture("github_traffic_views.json"),
            "clones": fixture("github_traffic_clones.json"),
            "referrers": fixture("github_traffic_referrers.json"),
            "paths": fixture("github_traffic_paths.json"),
        }

    def source_of(self, url: str) -> str:
        if url.startswith(collect.CRATES_API):
            return "crates_io"
        if url.startswith(collect.PYPISTATS_API):
            return "pypi"
        if url.startswith(collect.NPM_API):
            return "npm"
        return "github_traffic" if "/traffic/" in url else "github"

    def __call__(self, url: str, headers: dict | None = None):
        self.calls.append((url, dict(headers or {})))
        if self.source_of(url) in self.fail:
            raise collect.FetchError(f"HTTP 503 for {url}")
        p = self.payloads
        if m := re.fullmatch(r".*/crates/([^/?]+)", url):
            data = copy.deepcopy(p["crate"])
            name = m.group(1)
            data["crate"]["name"] = data["crate"]["id"] = name
            data["crate"]["downloads"] = 100 + 10 * CONFIG["crates"].index(name)
            return data
        if url.endswith("/downloads") and "crates.io" in url:
            return copy.deepcopy(p["downloads"])
        if "/reverse_dependencies" in url:
            return copy.deepcopy(p["revdeps"]) if "page=1&" in url else {"dependencies": [], "versions": [], "meta": {"total": 3}}
        table = [
            ("/recent", "pypi_recent"), ("/overall", "pypi_overall"), ("/system", "pypi_system"),
            ("/point/last-week/", "npm_week"), ("/point/last-month/", "npm_month"), ("/range/", "npm_range"),
            ("/releases", "gh_releases"), ("/traffic/views", "views"), ("/traffic/clones", "clones"),
            ("/popular/referrers", "referrers"), ("/popular/paths", "paths"),
        ]
        for needle, key in table:
            if needle in url:
                payload = copy.deepcopy(p[key])
                if key == "gh_releases" and "page=1" not in url:
                    return []
                return payload
        if re.fullmatch(r".*/repos/[^/]+/[^/]+", url):
            return copy.deepcopy(p["gh_repo"])
        raise AssertionError(f"unexpected URL {url}")


class Workspace:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        self.log: list[str] = []

    def run(self, api, now=NOW, env=None):
        env = {"TRAFFIC_TOKEN": SECRET} if env is None else env
        return collect.run(CONFIG, self.data, api, env, now, SCHEMA, log=self.log.append)

    def summary(self):
        return json.loads((self.data / "summary.json").read_text(encoding="utf-8"))

    def raw(self, date: str):
        return json.loads((self.data / "raw" / f"{date}.json").read_text(encoding="utf-8"))

    def series(self, name: str):
        return collect.read_series(self.data / "series" / f"{name}.csv")

    def snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.data)): p.read_bytes() for p in sorted(self.data.rglob("*")) if p.is_file()}

    def cleanup(self):
        self._tmp.cleanup()


class TestCase(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)


class FullRunTests(TestCase):
    def test_summary_matches_schema_and_contract(self):
        self.assertEqual(self.ws.run(FakeApi()), 0)
        s = self.ws.summary()
        self.assertEqual(collect.validate(s, SCHEMA), [])
        self.assertEqual(collect.check_summary(s, SCHEMA), [])
        self.assertEqual(s["schema"], 1)
        self.assertEqual(s["generated_at"], "2026-11-16T03:17:00Z")
        self.assertEqual(
            s["totals"],
            {
                "crates_io": 1650,
                "pypi_without_mirrors": 100,
                "pypi_with_mirrors": 295,
                "npm": 42,
                "github_release_assets": 8,
                "crates_io_reverse_dependencies": 2,
            },
        )
        self.assertEqual([c["name"] for c in s["crates"]], CONFIG["crates"])
        self.assertEqual(s["crates"][0], {"name": "cadkit", "downloads": 100, "recent_90d": 90, "max_version": "0.5.3", "reverse_dependencies": 2})
        self.assertEqual(s["pypi"]["os_without_mirrors"], {"Linux": 50, "Darwin": 5, "Windows": 8, "other": 2, "null": 35})
        self.assertEqual((s["pypi"]["last_day"], s["pypi"]["last_week"], s["pypi"]["last_month"]), (46, 367, 3489))
        self.assertEqual(s["npm"], {"package": "cadkit-wasm", "last_week": 269, "last_month": 1203})
        gh = s["github"]
        self.assertEqual((gh["stars"], gh["forks"], gh["open_issues"], gh["watchers"]), (6, 1, 2, 2))
        self.assertNotIn("v0.5.4", [a["tag"] for a in gh["release_assets"]], "drafts must be excluded")
        self.assertEqual(gh["traffic_14d"], {"views": 34, "unique_views": 13, "clones": 290, "unique_clones": 150})
        self.assertTrue(all(st["ok"] for st in s["sources"].values()))

    def test_series_values(self):
        self.ws.run(FakeApi())
        s = self.ws.summary()["series"]
        crates = dict(s["crates_io"])
        # 11 crates x (version + extra downloads per day); leading zero days dropped.
        self.assertEqual(s["crates_io"][0], ["2026-11-01", 77])
        self.assertEqual(crates["2026-11-05"], 0, "days inside the window without downloads are zero")
        self.assertEqual((crates["2026-11-14"], crates["2026-11-15"], crates["2026-11-16"]), (66, 77, 22))
        self.assertEqual(s["pypi_without_mirrors"], [["2026-11-13", 60], ["2026-11-14", 0], ["2026-11-15", 40]])
        self.assertEqual(s["npm"], [["2026-11-14", 12], ["2026-11-15", 30], ["2026-11-16", 0]])
        self.assertEqual(s["github_views"][-1], ["2026-11-16", 4, 1])
        self.assertEqual(s["github_clones"], [["2026-11-15", 250, 140], ["2026-11-16", 40, 10]])

    def test_requests_use_configured_names(self):
        api = FakeApi()
        self.ws.run(api)
        urls = [u for u, _ in api.calls]
        for crate in CONFIG["crates"]:
            self.assertIn(f"{collect.CRATES_API}/crates/{crate}", urls)
        self.assertTrue(any(u.endswith(f"/repos/{CONFIG['github_repo']}") for u in urls))
        self.assertTrue(any(CONFIG["npm_package"] in u for u in urls))
        self.assertTrue(any(f"/packages/{CONFIG['pypi_package']}/" in u for u in urls))

    def test_idempotent_same_day(self):
        self.ws.run(FakeApi())
        first = self.ws.snapshot()
        self.assertEqual(self.ws.run(FakeApi()), 0)
        self.assertEqual(first, self.ws.snapshot())
        self.assertEqual(sorted(p for p in first if p.startswith("raw")), ["raw/2026-11-16.json"])


class MergeTests(TestCase):
    def test_merge_rows_keeps_old_days_and_updates_overlap(self):
        old = {"2026-01-01": {"a": 1, "b": 2}, "2026-01-02": {"a": 3, "b": None}}
        new = {"2026-01-02": {"a": 5, "b": 6}, "2026-01-03": {"a": 7}}
        merged = collect.merge_rows(old, new)
        self.assertEqual(merged, {"2026-01-01": {"a": 1, "b": 2}, "2026-01-02": {"a": 5, "b": 6}, "2026-01-03": {"a": 7}})
        self.assertEqual(old["2026-01-02"], {"a": 3, "b": None}, "inputs are not mutated")

    def test_csv_round_trip_and_column_union(self):
        path = self.ws.data / "series" / "x.csv"
        collect.merge_series(path, ["a"], {"2026-01-02": {"a": 2}, "2026-01-01": {"a": 1}})
        collect.merge_series(path, ["b"], {"2026-01-02": {"b": 9}})
        self.assertEqual(path.read_text(encoding="utf-8"), "date,b,a\n2026-01-01,,1\n2026-01-02,9,2\n")
        cols, rows = collect.read_series(path)
        self.assertEqual(cols, ["b", "a"])
        self.assertEqual(rows["2026-01-01"], {"b": None, "a": 1})

    def test_history_beyond_api_window_is_kept(self):
        self.ws.run(FakeApi())
        later = FakeApi()
        # 200 days later the first days are outside the pypistats, crates.io and
        # traffic windows; npm still reports 2026-11-15 with a corrected value.
        later.payloads["npm_range"]["downloads"] = [{"downloads": 31, "day": "2026-11-15"}, {"downloads": 5, "day": "2027-06-03"}]
        later.payloads["pypi_overall"]["data"] = [
            {"category": "without_mirrors", "date": "2027-06-03", "downloads": 9},
            {"category": "with_mirrors", "date": "2027-06-03", "downloads": 20},
        ]
        later.payloads["pypi_system"]["data"] = [{"category": "Linux", "date": "2027-06-03", "downloads": 9}]
        later.payloads["views"]["views"] = [{"timestamp": "2027-06-03T00:00:00Z", "count": 6, "uniques": 2}]
        later.payloads["downloads"] = {"version_downloads": [{"version": 1, "downloads": 1, "date": "2027-06-03"}], "meta": {"extra_downloads": []}}
        self.assertEqual(self.ws.run(later, now=NOW + dt.timedelta(days=200)), 0)
        s = self.ws.summary()
        self.assertEqual(s["series"]["npm"], [["2026-11-14", 12], ["2026-11-15", 31], ["2026-11-16", 0], ["2027-06-03", 5]])
        pypi = dict(s["series"]["pypi_without_mirrors"])
        self.assertEqual((pypi["2026-11-13"], pypi["2026-11-15"], pypi["2027-06-03"]), (60, 40, 9))
        self.assertEqual(s["totals"]["pypi_without_mirrors"], 109)
        self.assertEqual(s["pypi"]["os_without_mirrors"]["Linux"], 59)
        crates = dict(s["series"]["crates_io"])
        self.assertEqual((crates["2026-11-01"], crates["2026-11-16"], crates["2027-06-03"]), (77, 22, 11))
        views = {d: v for d, v, _ in s["series"]["github_views"]}
        self.assertEqual((views["2026-11-14"], views["2027-06-03"]), (10, 6))
        self.assertEqual(sorted(p.name for p in (self.ws.data / "raw").iterdir()), ["2026-11-16.json", "2027-06-04.json"])

    def test_missing_days_inside_window_become_zero(self):
        self.ws.run(FakeApi())
        later = FakeApi()
        later.payloads["pypi_overall"]["data"] = [
            {"category": "without_mirrors", "date": "2026-11-16", "downloads": 9},
        ]
        self.ws.run(later, now=NOW + dt.timedelta(days=1))
        _, rows = self.ws.series("pypi")
        # pypistats omits zero days; a day it still covers but no longer lists had none.
        self.assertEqual((rows["2026-11-15"]["without_mirrors"], rows["2026-11-16"]["without_mirrors"]), (0, 9))
        self.assertEqual(rows["2026-11-15"]["with_mirrors"], 0)

    def test_crates_daily_sums_versions_and_extra(self):
        section = collect.fetch_crates(FakeApi(), {**CONFIG, "crates": ["cadkit"]}, NOW.date())
        self.assertEqual(section["daily"]["cadkit"], {"2026-11-01": 7, "2026-11-14": 6, "2026-11-15": 7, "2026-11-16": 2})
        self.assertEqual(section["crates"][0]["reverse_dependents"], ["cadkit-scene", "plan-viewer", "survey-tools"])

    def test_reverse_dependencies_paginate_and_exclude_own(self):
        pages = {
            1: {"dependencies": [{"version_id": 1}], "versions": [{"id": 1, "crate": "cadkit-gpu"}], "meta": {"total": 2}},
            2: {"dependencies": [{"version_id": 2}], "versions": [{"id": 2, "crate": "outside"}], "meta": {"total": 2}},
        }
        get = lambda url, headers=None: pages[int(re.search(r"page=(\d+)", url).group(1))]
        self.assertEqual(collect.fetch_reverse_dependents(get, "cadkit-core"), {"cadkit-gpu", "outside"})


class FailureTests(TestCase):
    def test_partial_failure_keeps_last_known_values(self):
        self.ws.run(FakeApi())
        before = self.ws.summary()
        later = NOW + dt.timedelta(days=1)
        self.assertEqual(self.ws.run(FakeApi(fail={"pypi"}), now=later), 0)
        s = self.ws.summary()
        self.assertEqual(collect.check_summary(s, SCHEMA), [])
        self.assertFalse(s["sources"]["pypi"]["ok"])
        self.assertIn("HTTP 503", s["sources"]["pypi"]["error"])
        self.assertEqual(s["sources"]["pypi"]["last_success_at"], "2026-11-16T03:17:00Z")
        self.assertTrue(s["sources"]["npm"]["ok"])
        self.assertEqual(s["pypi"], before["pypi"])
        self.assertEqual(s["totals"]["pypi_without_mirrors"], before["totals"]["pypi_without_mirrors"])
        self.assertTrue(any(n.startswith("pypi: failed") for n in s["notes"]))
        raw = self.ws.raw("2026-11-17")
        self.assertFalse(raw["sources"]["pypi"]["ok"])
        self.assertNotIn("pypi", raw["data"])

    def test_failed_rerun_same_day_keeps_earlier_success(self):
        self.ws.run(FakeApi())
        self.assertEqual(self.ws.run(FakeApi(fail={"crates_io"})), 0)
        raw = self.ws.raw("2026-11-16")
        self.assertTrue(raw["sources"]["crates_io"]["ok"])
        self.assertIn("crates_io", raw["data"])
        self.assertFalse(self.ws.summary()["sources"]["crates_io"]["ok"])

    def test_everything_failed_exits_non_zero_and_writes_nothing(self):
        api = FakeApi(fail={"crates_io", "pypi", "npm", "github", "github_traffic"})
        self.assertEqual(self.ws.run(api), 1)
        self.assertFalse(self.ws.data.exists())

    def test_first_run_without_history_for_a_source_writes_no_summary(self):
        self.assertEqual(self.ws.run(FakeApi(fail={"npm"})), 2)
        self.assertFalse((self.ws.data / "summary.json").exists())
        self.assertTrue((self.ws.data / "raw" / "2026-11-16.json").exists())

    def test_unexpected_payload_shape_is_a_source_failure(self):
        api = FakeApi()
        api.payloads["npm_week"] = {"error": "package not found"}
        self.ws.run(api)  # first run: npm never succeeded
        self.assertIn("KeyError", self.ws.raw("2026-11-16")["sources"]["npm"]["error"])

    def test_traffic_skipped_without_token_keeps_previous_traffic(self):
        self.ws.run(FakeApi())
        before = self.ws.summary()["github"]["traffic_14d"]
        api = FakeApi()
        self.assertEqual(self.ws.run(api, now=NOW + dt.timedelta(days=1), env={}), 0)
        self.assertFalse(any("/traffic/" in u for u, _ in api.calls))
        s = self.ws.summary()
        st = s["sources"]["github_traffic"]
        self.assertTrue(st["skipped"])
        self.assertFalse(st["ok"])
        self.assertEqual(st["last_success_at"], "2026-11-16T03:17:00Z")
        self.assertEqual(s["github"]["traffic_14d"], before)
        self.assertEqual(len(s["series"]["github_views"]), 3)

    def test_traffic_null_when_never_captured(self):
        self.assertEqual(self.ws.run(FakeApi(), env={}), 0)
        s = self.ws.summary()
        self.assertIsNone(s["github"]["traffic_14d"])
        self.assertEqual(s["series"]["github_views"], [])
        self.assertEqual(collect.validate(s, SCHEMA), [])


class SecretTests(TestCase):
    def test_tokens_never_reach_outputs(self):
        api = FakeApi()
        self.ws.run(api, env={"TRAFFIC_TOKEN": SECRET, "GITHUB_TOKEN": SECRET + "x"})
        traffic_auth = [h.get("Authorization") for u, h in api.calls if "/traffic/" in u]
        self.assertTrue(traffic_auth and all(a == f"Bearer {SECRET}" for a in traffic_auth))
        for name, content in self.ws.snapshot().items():
            text = content.decode("utf-8")
            self.assertNotIn(SECRET, text, name)
            self.assertNotIn("Authorization", text, name)
            self.assertNotIn("Bearer", text, name)
        self.assertNotIn(SECRET, "\n".join(self.ws.log))

    def test_error_messages_are_scrubbed(self):
        def leaky(url, headers=None):
            raise RuntimeError(f"boom {SECRET}")

        self.assertEqual(self.ws.run(leaky), 1)
        self.assertNotIn(SECRET, "\n".join(self.ws.log))

    def test_non_traffic_github_calls_do_not_use_traffic_token(self):
        api = FakeApi()
        self.ws.run(api, env={"TRAFFIC_TOKEN": SECRET})
        for url, headers in api.calls:
            if "api.github.com" in url and "/traffic/" not in url:
                self.assertNotIn("Authorization", headers)

    def test_raw_snapshot_has_no_user_records(self):
        self.ws.run(FakeApi())
        text = (self.ws.data / "raw" / "2026-11-16.json").read_text(encoding="utf-8")
        for needle in ("fixture-user", "avatar", "uploader", "published_by"):
            self.assertNotIn(needle, text)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class HttpClientTests(unittest.TestCase):
    def make(self, outcomes):
        self.sleeps: list[float] = []
        self.requests = []
        self.t = 0.0

        def urlopen(request, timeout):
            self.requests.append((request, timeout))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(json.dumps(outcome).encode())

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.t += seconds

        return collect.HttpClient("ua-test", sleep=sleep, clock=lambda: self.t, urlopen=urlopen)

    @staticmethod
    def http_error(code, retry_after=None):
        headers = email.message.Message()
        if retry_after:
            headers["Retry-After"] = retry_after
        return urllib.error.HTTPError("https://crates.io/x", code, "err", headers, io.BytesIO(b""))

    def test_retries_with_backoff_then_succeeds(self):
        client = self.make([self.http_error(503), urllib.error.URLError("reset"), {"ok": 1}])
        self.assertEqual(client.get_json("https://crates.io/api/v1/crates/x"), {"ok": 1})
        self.assertEqual(self.sleeps, [2.0, 4.0])
        request, timeout = self.requests[0]
        self.assertEqual(request.get_header("User-agent"), "ua-test")
        self.assertEqual(timeout, 30.0)

    def test_honours_retry_after(self):
        client = self.make([self.http_error(429, "7"), {"ok": 1}])
        client.get_json("https://crates.io/a")
        self.assertEqual(self.sleeps, [7.0])

    def test_no_retry_on_404_and_message_has_no_headers(self):
        client = self.make([self.http_error(404)])
        with self.assertRaises(collect.FetchError) as ctx:
            client.get_json("https://api.github.com/repos/a/b", headers={"Authorization": f"Bearer {SECRET}"})
        self.assertEqual(str(ctx.exception), "HTTP 404 for https://api.github.com/repos/a/b")
        self.assertEqual(self.sleeps, [])

    def test_gives_up_after_retries(self):
        client = self.make([self.http_error(500)] * 4)
        with self.assertRaises(collect.FetchError):
            client.get_json("https://pypistats.org/api/x")
        self.assertEqual(self.sleeps, [2.0, 4.0, 8.0])

    def test_at_most_one_request_per_second_per_host(self):
        client = self.make([{"a": 1}, {"a": 2}, {"a": 3}])
        client.get_json("https://crates.io/a")
        client.get_json("https://crates.io/b")
        client.get_json("https://api.npmjs.org/c")
        self.assertEqual(self.sleeps, [1.0], "second crates.io call waits; other host does not")


class ValidatorTests(unittest.TestCase):
    def test_schema_declares_draft_2020_12(self):
        self.assertEqual(SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_rejects_bad_summaries(self):
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        ws.run(FakeApi())
        good = ws.summary()
        cases = [
            (lambda s: s["totals"].pop("npm"), "missing required 'npm'"),
            (lambda s: s["totals"].__setitem__("npm", True), "expected integer"),
            (lambda s: s["totals"].__setitem__("npm", -1), "below minimum"),
            (lambda s: s["series"]["npm"].append(["2026-13", 1]), "does not match"),
            (lambda s: s["series"]["github_views"].append(["2026-11-17", 1]), "fewer than minItems"),
            (lambda s: s["series"]["npm"].append(["2026-11-17", 1, 2]), "not allowed"),
            (lambda s: s.__setitem__("schema", 2), "expected const"),
            (lambda s: s["github"].__setitem__("traffic_14d", {"views": 1}), "missing required 'unique_views'"),
            (lambda s: s["sources"]["npm"].__setitem__("ok", "yes"), "expected boolean"),
        ]
        for mutate, expected in cases:
            bad = copy.deepcopy(good)
            mutate(bad)
            errors = collect.validate(bad, SCHEMA)
            self.assertTrue(any(expected in e for e in errors), (expected, errors))

    def test_consistency_checks(self):
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        ws.run(FakeApi())
        bad = ws.summary()
        bad["totals"]["crates_io"] += 1
        bad["series"]["npm"].reverse()
        errors = collect.check_summary(bad, SCHEMA)
        self.assertIn("totals.crates_io != sum(crates[].downloads)", errors)
        self.assertIn("series.npm: dates are not strictly ascending", errors)


if __name__ == "__main__":
    unittest.main()
