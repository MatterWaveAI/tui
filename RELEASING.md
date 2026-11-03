# Releasing `matterwave-tui`

This document describes the release process for `matterwave-tui`. Day-to-day
development happens on `develop`, and only commits reachable from `main` may be
tagged and published to PyPI.

## 1. Prepare the Release on `develop`

1. **Bump the package version**:
   - `pyproject.toml` declares `dynamic = ["version"]` and reads `__version__`
     from `src/matterwave_tui/__init__.py` (`[tool.hatch.version]`). Update the
     version string in `src/matterwave_tui/__init__.py` (and any minimum
     dependency floors in `pyproject.toml` if needed) to the target `X.Y.Z`.
2. **Update `CHANGELOG.md`**:
   - Move the `[Unreleased]` entries into a new `## [X.Y.Z] - YYYY-MM-DD`
     section and update the comparison links at the bottom of `CHANGELOG.md`.
   - Verify the release helper accepts the new tag and extracts release notes:
     ```sh
     python scripts/release.py check-tag vX.Y.Z
     python scripts/release.py notes X.Y.Z
     ```
3. **Run the local quality gate**:
   ```sh
   uv run ruff check .
   sh scripts/check-ticket-ids.sh
   uv run ruff format --check .
   uv run mypy --strict
   uv run pytest -q --ignore=tests/e2e
   ```
4. Commit and push the release preparation to `develop`:
   ```sh
   git push origin develop
   ```

## 2. Merge `develop` into `main`

Merge `develop` into `main` via a pull request (or fast-forward merge after CI
passes on `develop`):

```sh
git checkout main
git pull origin main
git merge --ff-only origin/develop
git push origin main
```

Wait for the `CI` workflow (`.github/workflows/ci.yml`) to pass on `main`.

## 3. Tag `vX.Y.Z` on `main`

Create and push an annotated tag on `main`:

```sh
git checkout main
git pull origin main
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

> **Branch Guard**: `.github/workflows/publish-pypi.yml` enforces that every
> release tag points to a commit on `origin/main` via:
> ```sh
> git fetch origin main
> git merge-base --is-ancestor "$GITHUB_SHA" origin/main
> ```
> Tags created on `develop` or unmerged feature branches will be rejected before
> building or publishing.

## 4. Verify the PyPI Publish Workflow

1. Open the **Actions** tab on GitHub and inspect the `publish-pypi` workflow
   run triggered by `vX.Y.Z`.
2. Confirm all three jobs succeed:
   - `build`: verifies `origin/main` ancestry, checks that the tag matches the
     package version (`python scripts/release.py check-tag "$TAG"`), runs lint,
     typecheck, and tests, builds the wheel/sdist, exports the CycloneDX SBOM
     (`sbom.cdx.json`), and extracts `notes.md` from `CHANGELOG.md`.
   - `publish`: uploads the distribution to PyPI using OIDC trusted publishing.
   - `release`: creates the GitHub Release for `vX.Y.Z` with the changelog
     notes, wheel/sdist artifacts, and CycloneDX SBOM.
3. Verify the published package can be installed from PyPI:
   ```sh
   uv tool install --upgrade matterwave-tui
   matterwave-tui --version
   ```
