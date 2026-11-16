# cadkit-stats

Public, aggregate usage statistics for [cadkit](https://github.com/TNYCL/cadkit),
collected once a day by a GitHub Actions workflow and published as
[`data/summary.json`](data/summary.json) for <https://cadkit.org/stats/>.

The collector is a single Python file ([`collect.py`](collect.py)) that uses the
standard library only. Package names and the GitHub repository come from
[`config.json`](config.json), so moving the repository to another owner is a
one-line change there.

## What is collected

| Source | Endpoint | Data |
|---|---|---|
| crates.io | `/api/v1/crates/<name>`, `/downloads`, `/reverse_dependencies` | For every cadkit crate (`cadkit`, `cadkit-core`, `cadkit-dwg`, `cadkit-dgn`, `cadkit-dxf`, `cadkit-gml`, `cadkit-geom`, `cadkit-scene`, `cadkit-gpu`, `cadkit-cli`, `cadkit-capi`): all-time downloads, recent (90-day) downloads, `max_version`, per-day downloads (version downloads plus `extra_downloads`, about 90 days), and reverse dependencies excluding cadkit's own crates. |
| pypistats.org | `/api/packages/pycadkit/recent`, `/overall`, `/system?mirrors=false` | `pycadkit`: last day / week / month, daily downloads with and without mirrors, daily downloads by operating system without mirrors. |
| api.npmjs.org | `/downloads/point/last-week`, `/last-month`, `/downloads/range/<18 months>` | `cadkit-wasm`: last week, last month and daily downloads. |
| GitHub REST | `/repos/TNYCL/cadkit`, `/releases` | Stars, forks, open issues, watchers (subscribers) and the download count of every asset of every published release. |
| GitHub traffic | `/traffic/views`, `/traffic/clones`, `/traffic/popular/referrers`, `/traffic/popular/paths` | Only when `TRAFFIC_TOKEN` is set (see below). 14-day totals, daily views and clones, top referrers and paths. |

Requests are polite: the User-Agent is `cadkit-stats (https://github.com/TNYCL/cadkit-stats)`,
at most one request per second per host, a 30 s timeout and up to three retries
with exponential backoff (honouring `Retry-After`) on 429 and 5xx responses.

## Output

```
data/
  raw/YYYY-MM-DD.json   one snapshot per UTC day (extracted fields only)
  series/*.csv          merged daily history, "date,value(s)", ascending
  summary.json          what the website reads
```

- **Raw snapshots** hold the fields extracted from each response, never whole
  responses (no user records, avatars or headers). Re-running on the same day
  overwrites that day's file; a source that fails on a re-run keeps the data an
  earlier run of the same day fetched.
- **Series** (`crates_io.csv` with a `total` and one column per crate,
  `pypi.csv`, `pypi_system.csv`, `npm.csv`, `github_views.csv`,
  `github_clones.csv`, and `github_repo.csv` with one point-in-time row per
  collection day). Merging keeps every day that has fallen out of an API window
  and replaces overlapping days with the newest values. Inside an API window a
  day the API no longer lists is stored as 0 (crates.io and pypistats omit days
  without downloads).
- **Summary**: [`schema/summary.schema.json`](schema/summary.schema.json)
  (JSON Schema draft 2020-12). `collect.py` validates the summary against it,
  plus cross-field checks, before writing. Additive fields beyond the site
  contract: `totals.crates_io_reverse_dependencies`,
  `crates[].reverse_dependencies`, `github.repo`,
  `github.popular_referrers_14d`, `github.popular_paths_14d` and `sources`.

### Source status and failures

`summary.sources` has one entry per source:
`{"ok": bool, "fetched_at": iso, "error": str|null, "last_success_at": iso|null}`
(plus `"skipped": true` for traffic without a token). `fetched_at` is the start
time of the collection run.

- A source that fails keeps its last known values in the summary, is marked
  `ok: false` with the error, and a note says which run the values come from.
- The collector exits 0 when at least one source succeeded and a valid summary
  was written, 1 when every source failed (nothing is written), and 2 when a
  source has never succeeded or the summary failed validation (the summary is
  not written).

## How to read the numbers

- **Downloads are not users.** CI pipelines, mirrors, caches, dependency bots and
  rebuilds all download packages. Use the numbers for trends, not head counts.
- **crates.io** totals are all-time. Per-day data is only available for 90 days,
  which is why the collector stores it daily. Per-day counts and totals are
  updated on different schedules, so the daily series can briefly sum to
  slightly more than the total.
- **PyPI** numbers come from pypistats.org, which derives them from the public
  PyPI BigQuery dataset. "Without mirrors" excludes known mirroring clients and
  is the better signal; "with mirrors" is shown for completeness. A large
  `null` operating-system share is normal: many installers and CI tools do not
  report a platform.
- **npm** counts are registry downloads of `cadkit-wasm` tarballs.
- **GitHub traffic** is only kept by GitHub for a rolling 14 days. Capturing it
  every day is the only way to build a longer history; the view and clone series
  contain only days the collector captured. `watchers` are repository
  subscribers, not stars, and `open_issues` includes open pull requests.
- The most recent day in each daily series may be incomplete; it is corrected by
  the next run.

## Schedule

[`.github/workflows/collect.yml`](.github/workflows/collect.yml) runs daily at
03:17 UTC and on manual dispatch. It runs the tests, runs `collect.py`, commits
`data/` as `github-actions[bot]` only when it changed, and then optionally asks
Cloudflare Pages to rebuild the website. Its only permission is
`contents: write`; all actions are pinned to full commit SHAs.
[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs the tests on
pushes and pull requests.

## Secrets

Both secrets are optional. Set them in this repository under
Settings → Secrets and variables → Actions, or with `gh secret set <NAME> -R TNYCL/cadkit-stats`
(the value is read from stdin; never paste it into a file or a command line that is logged).

- **`TRAFFIC_TOKEN`**: GitHub's traffic endpoints are only available to people
  with push access, so the workflow's own token cannot read them for another
  repository. Create a fine-grained personal access token with
  - resource owner: the owner of `TNYCL/cadkit`,
  - repository access: *Only select repositories* → `TNYCL/cadkit`,
  - repository permissions: **Administration: Read-only** (Metadata: Read-only is
    added automatically); nothing else,
  - an expiry date, with a reminder to rotate it.

  Read-only administration access lets the token read traffic and repository
  settings but not change anything or read private code. Without the secret the
  traffic step is skipped, earlier traffic stays in the summary, and
  `sources.github_traffic` records `skipped`. Public repository and release data
  use the workflow's `GITHUB_TOKEN`.
- **`PAGES_DEPLOY_HOOK`**: a Cloudflare Pages deploy hook URL for the website
  project (Pages project → Settings → Builds & deployments → Deploy hooks). The
  URL itself is the credential, so it is kept as a secret and never printed. After
  a data commit the workflow waits about five minutes, because
  `raw.githubusercontent.com` caches files for up to five minutes, and then
  POSTs to the hook.

## Privacy

Only aggregate counts published by the registries and GitHub are stored. The
collector does not store user names, e-mail addresses, avatars, IP addresses or
any other personal data, and never writes tokens, headers or credentials to
`data/` or to logs. GitHub's popular referrers are site names (for example
`github.com`) and popular paths are pages of the public repository.

## Development

```sh
python3 -m unittest discover -s tests -t .   # offline, synthetic fixtures
python3 collect.py                           # real collection into data/
TRAFFIC_TOKEN=... python3 collect.py         # include GitHub traffic (keep the token out of shell history)
```

Python 3.12 or newer, no third-party packages.

## License

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or
[MIT license](LICENSE-MIT) at your option.
