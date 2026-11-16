# AGENTS.md — working on cadkit-stats

This repository collects public, aggregate statistics for cadkit
(https://github.com/TNYCL/cadkit) and publishes them as `data/summary.json` for
https://cadkit.org/stats/.

- Code, comments, docs and commit messages are in English.
- Python 3.12+, **standard library only**. Do not add third-party dependencies.
- Run the tests before committing: `python -m unittest discover -s tests -t .`
  Tests are offline and use synthetic fixtures in `tests/fixtures/`.
- `data/summary.json` must validate against `schema/summary.schema.json`.
  Changes to the contract are additive only; removing or renaming a field needs
  a new `schema` number and a coordinated change on the site.
- Never commit, log or print secrets (tokens, deploy hook URLs, auth headers).
  Secrets live only in GitHub Actions secrets and process environments.
- Collect aggregate counts only; never store user names, e-mails, avatars or
  other personal data from API responses.
- Be polite to the APIs: keep the User-Agent from `config.json`, at most one
  request per second per host, timeouts and bounded retries.
- Never add `Co-Authored-By` or "Generated with" lines to commits, PRs or files.
