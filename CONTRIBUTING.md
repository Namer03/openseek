# Contributing

Thanks for helping. Please read `CODE_OF_CONDUCT.md` first. For security issues, follow `SECURITY.md` instead of opening an issue.

## Development setup

```sh
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-test.txt     # Windows: .venv\Scripts\pip
```

Python 3.12 or newer. The test fixtures are generated at the start of a test session with `ffmpeg`, which must be on your `PATH`.

## Running the tests

```sh
cd backend
.venv/bin/python -m pytest tests -q -p no:cacheprovider      # Windows: .venv\Scripts\python
```

Tests start their own servers on free local ports with temporary data directories; do not point them at a real installation.
Some tests depend on optional local fixtures or on other projects and may be skipped or need environment variables; see the test file headers.
The benchmark harness has its own tests in `bench/tests` (`bench/requirements.txt`). The desktop app is in `desktop/` (PySide6).

## Code style

* Python 3.12, type hints where they help, `from __future__ import annotations`. Match the surrounding style; keep functions small and comments factual.
* No `shell=True`, no `os.system`; subprocesses take argument lists.
* Never log URLs, tokens, headers or ids from clients; log salted host tags (`logutil.host_tag`). Never persist stream URLs.
* Any change to the outbound policy, authentication, listeners or Contributor Mode needs tests, including negative tests.
* Do not add an environment switch that weakens the destination policy.
* Keep dependencies minimal and pinned; new dependencies need a licence check.

## Pull requests

* One logical change per pull request, with tests and a CHANGELOG entry under "Unreleased".
* Describe what changed and how you tested it. State what you could not test.
* Update documentation (`docs/`, README configuration table) when behaviour or settings change.
* Be patient with review; this is a volunteer project.

## Fixtures and media

Do not add copyrighted media, real media links, real debrid/CDN URLs, tokens, invitation codes or personal data (names, hostnames, home addresses) to
the repository, tests or issues. Use generated or clearly licensed media and placeholder hosts such as `example.com`, `192.0.2.x` and `203.0.113.x`.

## Licence

The project licence has not been chosen yet (see `docs/LICENSE-OPTIONS.md`). Do not submit code you do not have the right to contribute.
