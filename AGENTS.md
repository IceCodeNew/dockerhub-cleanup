# Repository guidance

## Documentation boundaries

- `docs/requirements.md` defines product behavior and safety requirements.
- `docs/design.md` defines architecture, adapters, data flow, and known API limits.
- `README.md` is the operator-facing setup and usage guide.
- Update the relevant document when behavior, authentication, or an external endpoint changes.

## Architecture boundaries

- Keep policy selection in `dockerhub_cleanup/domain.py` independent of Docker Hub, HTTP, subprocesses, and environment variables.
- Keep orchestration in `dockerhub_cleanup/service.py`; adapters should translate external data and errors, not decide cleanup policy.
- Use `DockerHubClient` for documented Docker Hub repository and tag operations.
- Isolate the undocumented Image Management endpoint and browser Cookie handling in `ImageManagementClient`.
- Isolate `crane` execution and its temporary `DOCKER_CONFIG` in `CraneClient`.
- Complete discovery and planning before performing any deletion.

## Safety and secrets

- Preserve dry-run as the default. Applying a plan must require both `--apply` and an exact namespace confirmation.
- Never log, persist, or pass `DH_PAT` or `DH_COOKIE` in command-line arguments.
- Never pass the Docker Hub browser Cookie to `crane` or the documented Hub API.
- Treat incomplete pagination, malformed responses, and external command failures as safe failures.
- Do not add unit tests that contact Docker Hub or another live service. Use fake transports and fake subprocess runners.

## Code and tests

- Support the Python versions declared in `pyproject.toml` and CI.
- Keep code comments and docstrings concise and in English.
- Add a unit test in the same commit as every behavior change.
- Maintain 100% statement and branch coverage for `dockerhub_cleanup`.
- Avoid runtime dependencies unless the standard library cannot express the requirement clearly and safely.

## Commits

- Use Conventional Commit subjects.
- Keep commits minimal, focused, and independently reviewable.
- Order implementation commits by dependency: domain logic, external adapters, orchestration, then user-facing workflows.
- Keep configuration and documentation changes separate from feature commits when they can stand alone.
- Preserve unrelated user changes and do not rewrite published history without explicit permission.

## Validation

Run these checks before committing behavior changes:

```bash
mise run coverage
mise run lint
SKIP=no-commit-to-branch prek run --all-files
```

Run `mise exec -- uv lock --check` after dependency or package metadata changes. Run `mise exec -- uv build` after packaging changes.
