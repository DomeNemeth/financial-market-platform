# ADR-0010: Dependency and runtime pinning

**Date:** 2026-08-02
**Status:** Accepted

## Context

Phase 1 shipped with `dbt-core 1.12.0b3` — a pre-release — installed in the
project venv. Nobody chose it. `pyproject.toml` declared `dbt-postgres>=1.7`,
unbounded, and pip resolved a beta.

The mechanism is worth recording, because it is not obvious and it will recur.
pip does not install pre-releases by default, **but** it becomes eligible to do so
for a given package when a version specifier in the dependency graph itself
mentions a pre-release. `dbt-postgres` declares `dbt-core>=1.8.0rc1`. That `rc1`
is enough to put every dbt-core pre-release on the table, so with no upper bound
the resolver took the newest thing it could see.

A beta build of the transform engine is not something to leave in a portfolio
project, and the failure mode is bad: it is invisible until someone runs
`dbt --version`, and it silently changes which YAML syntax is valid.

Fixing it surfaced a second, connected problem. The obvious move — upgrade to the
now-released `dbt-core 1.12.0` — does not work: **1.12.0 hard-requires
`dbt-core-experimental-parser>=2.0.0a4`, itself an alpha.** Upgrading would have
swapped one pre-release for another while appearing to fix the problem.

Dropping to the 1.11 line then surfaced a third: `dbt-core 1.11.x` pins
`mashumaro<3.15`, and `mashumaro 3.14` raises `UnserializableField` at import
time under Python 3.14. The venv was on Python 3.14.2 purely because that is what
was on PATH when it was created. So "stable dbt" and "Python 3.14" were mutually
exclusive.

Meanwhile the repository asserted three different things about the runtime:
`requires-python = ">=3.11"`, `[tool.mypy] python_version = "3.11"`, and
`docker/Dockerfile.api` building `FROM python:3.11-slim` — while the interpreter
actually running tests and dbt was 3.14.2.

## Decision

**Pin both dbt packages to the 1.11 line**, the newest whose entire dependency
tree is stable:

```toml
"dbt-core>=1.11,<1.12",
"dbt-postgres>=1.11,<1.12",
```

`dbt-core` is pinned directly rather than left to `dbt-postgres` to imply, since
that indirection is exactly what allowed the beta in.

**Standardise on Python 3.11**, and rebuild the venv on it. 3.11 was chosen over
a newer 3.12/3.13 because it is what the container already runs and what every
declaration in the repository already claimed — so the fix makes three existing
statements true rather than requiring four edits and an image rebuild.

**Upper-bound anything whose transitive constraints can bite.** `mypy>=1.9,<2.0`:
mypy 2.x requires `pathspec>=1.0`, dbt-core requires `pathspec<0.13`, and the two
cannot coexist.

**Declare tools the repo actually shells out to.** `scripts/dbt.ps1` invokes
`dotenv.exe`, which only exists with `python-dotenv[cli]`. It had been present
only as a transitive dependency of `pydantic-settings` — which pulls the library
but not the console script's `click` dependency — so the wrapper worked by
accident and would have broken on any resolution change.

## Consequences

Good:

- `dbt --version` reports a stable release, and nothing in the tree is a
  pre-release.
- Dev and container run the same interpreter, so "works on my machine" and
  "works in the image" mean the same thing.
- Three previously-false claims in `pyproject.toml`, `[tool.mypy]`, and
  `CLAUDE.md` are now true without any of them being edited.
- The `accepted_values`-under-`arguments:` syntax in `_staging.yml` turned out to
  be valid on 1.11 as well, so no model change was needed. This was an open risk
  when the downgrade started.

Bad:

- Python 3.11 receives security-only fixes from October 2027. This will need
  revisiting, and the constraint to check first is whether dbt's mashumaro pin
  has moved.
- Pinning to `<1.12` means not getting dbt 1.12 features until its experimental
  parser dependency reaches a stable release.
- Upper bounds require maintenance. They are deliberate: an unbounded lower-only
  range is what caused this.

Neutral:

- Installing Python 3.11 on the dev machine required downloading the python.org
  installer directly. Both `winget` and `uv` hung — see the TLS note below.

## Alternatives Considered

**Upgrade to dbt-core 1.12.0.** Rejected: it depends on an alpha
(`dbt-core-experimental-parser>=2.0.0a4`), which is the same problem in newer
packaging. It also could not be installed here at all — that package's build
backend downloads a wheel from GitHub at build time using `urllib` against
`certifi`, which fails under this machine's TLS interception.

**Keep Python 3.14 and pin dbt to 1.12.0b3.** Rejected: shipping a beta transform
engine is precisely the thing being fixed.

**Move the container to Python 3.14 instead of the venv to 3.11.** Would have
given parity too, and on a newer runtime. Rejected because dbt does not run on
3.14 at all, so it would not have solved the actual blocker.

**Lock file (`uv.lock` / `pip-tools`).** The genuinely correct answer for
reproducibility, and a likely future change. Deliberately deferred: the immediate
problem was an unbounded *range*, and a lock file over a bad range just freezes
the bad resolution. Ranges first, lock second.

## Note: TLS interception on the dev machine

Documented here because it shaped the above and will affect any future tooling
change. Avast Web/Mail Shield MITMs HTTPS with its own root CA. That root is in
the Windows trust store, so browsers work, but anything verifying against a
bundled CA set fails:

- `winget install Python.Python.3.11` — msstore source fails certificate
  validation outright; the winget source hung indefinitely.
- `uv python install 3.11` — hung, including with `--native-tls`.
- `pip` in a *fresh* venv — `CERTIFICATE_VERIFY_FAILED`. The old venv worked only
  because `truststore` happened to already be installed in it.

Resolved by exporting the Windows root store to a PEM bundle and pointing pip at
it permanently:

```powershell
# regenerate if the trust store changes
$out = "$env:USERPROFILE\.certs\windows-root-ca.pem"
# ... export Cert:\LocalMachine\Root + Cert:\CurrentUser\Root as PEM ...
python -m pip config set --user global.cert $out
```

At runtime the application solves the same problem differently and correctly, via
`src/common/tls.py` calling `truststore.inject_into_ssl()`. Certificate
verification stays **on** in both cases — `verify=False` is never the fix.
