# Repository guidance

## Documentation ownership

- `README.md` is exclusively the operator guide: installation, authentication, preview, apply, exit behavior, and operational safety.
- `docs/requirements.md` defines product scope, observable behavior, safety guarantees, and non-goals.
- `docs/design.md` defines architecture, adapters, data flow, external API constraints, and test boundaries.
- `AGENTS.md` defines the development principles and workflow that contributors must follow.
- Update the owning document whenever behavior, authentication, architecture, an external endpoint, or contributor workflow changes. Do not duplicate details across documents unless an operator must know them to use the tool safely.

## Development workflow

1. Read the relevant requirements and design sections before changing behavior.
2. Inspect the working tree and preserve unrelated user changes.
3. Complete discovery and planning before editing. Keep the change focused on one concern.
4. Implement in dependency order: domain policy, external adapters, orchestration, then user-facing workflows.
5. Add or update unit tests in the same commit as every behavior change.
6. Update the document that owns any changed contract or constraint.
7. Run the required validation before committing.
8. Commit minimal, independently reviewable changes with Conventional Commit subjects.
9. Before publishing or updating a pull request, verify coverage and review the complete diff for scope, secrets, and generated artifacts.
10. Address all known valid findings and rerun validation before requesting another automated review.

## Architecture boundaries

- Keep policy selection in `dockerhub_cleanup/domain.py` independent of Docker Hub, HTTP, subprocesses, and environment variables.
- Keep orchestration in `dockerhub_cleanup/service.py`. Adapters translate external data and errors; they do not decide cleanup policy.
- Use `DockerHubClient` only for documented Docker Hub repository and tag operations.
- Isolate the undocumented Image Management endpoint and browser Cookie handling in `ImageManagementClient`.
- Isolate `crane` execution and its temporary `DOCKER_CONFIG` in `CraneClient`.
- Complete all discovery and planning before performing any deletion. Never apply a partial discovery result.

## Safety and secrets

- Preserve dry-run as the default. Applying a plan requires both `--apply` and an exact namespace confirmation.
- Never log, persist, commit, or pass `DH_PAT`, JWTs, or `DH_COOKIE` in command-line arguments.
- Never pass the Docker Hub browser Cookie to `crane` or the documented Hub API.
- Remove Docker Hub secret variables from subprocess environments unless that subprocess explicitly requires them.
- Keep external calls bounded by finite timeouts.
- Treat incomplete pagination, malformed responses, untrusted pagination origins, and external command failures as safe failures.
- Redact response bodies and credentials from operator-facing errors.
- Before making the repository public or publishing a release, scan the complete Git history and current tree for credentials and private data.

## Code and tests

- Support every Python version declared in `pyproject.toml` and CI.
- Keep runtime code compatible with the oldest supported Python version.
- Keep comments and docstrings concise and in English.
- Prefer the standard library; add a runtime dependency only when it is clearly safer or simpler than a standard-library implementation.
- Do not add unit tests that contact Docker Hub, another live service, or a real registry. Use fake transports and fake subprocess runners.
- Cover success, safe-failure, malformed-input, pagination, timeout, and secret-boundary behavior where applicable.
- Maintain 100% statement and branch coverage for `dockerhub_cleanup`.

## Commits and pull requests

- Use Conventional Commit subjects.
- Keep commits minimal, focused, and independently reviewable.
- Keep configuration and documentation changes separate from behavior changes when they can stand alone.
- Preserve unrelated changes and never rewrite published history without explicit permission.
- Keep each pull request focused on one concern and include direct coverage for its behavior.
- Do not mix unrelated coverage improvements or test-only cleanup into a behavior pull request.
- A pull request must not reduce statement or branch coverage.
- Prefer a merge strategy that preserves intentionally structured commits.

## Validation

Run these checks before committing behavior changes and before publishing a pull request:

```bash
mise run coverage
mise run lint
SKIP=no-commit-to-branch prek run --all-files
```

Run additional checks when their inputs change:

- Dependency or package metadata: `mise exec -- uv lock --check`
- Packaging configuration: `mise exec -- uv build`
- GitHub Actions: ensure both `actionlint` and `zizmor` pass through prek

All known failures must be understood and fixed before merge.
