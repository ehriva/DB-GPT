# Repository Guidelines

## Project Structure & Module Organization

This checkout contains the DB-GPT Python workspace and its Next.js web UI.

- `packages/dbgpt-core/src/dbgpt/`: core abstractions, agents, and workflows.
- `packages/dbgpt-ext/`, `dbgpt-serve/`, `dbgpt-app/`, `dbgpt-client/`, `dbgpt-sandbox/`, and `dbgpt-accelerator/`: integrations, services, application, client, sandbox, and acceleration packages.
- `web/`: TypeScript/React UI; assets in `web/public/`, translations in `web/locales/`.
- Package-local `tests/` directories and root `tests/`: Python tests; `web/utils/*.test.ts`: frontend utility tests.
- `configs/`, `examples/`, `docs/`, and `docker/`: configuration templates, usage examples, documentation, and deployment resources.

## Build, Test, and Development Commands

Use Python 3.11 (`.python-version`) and uv. Run backend commands from the repository root:

```sh
uv sync --all-packages --extra base --extra proxy_openai --extra rag --extra storage_chromadb --extra dbgpts
uv run dbgpt start webserver --config configs/dbgpt-local-glm.toml
uv build --all-packages
```

These install dependencies, start the configured backend, and build distributions. Configure the model before starting.

- `make test`: core Python tests; `make test-doc`: package doctests.
- `make mypy`: core package type checks; `make coverage`: core coverage report.
- `make fmt`: format Python and fix imports/lint findings.
- `make fmt-check`: formatting checks **also run lint autofixes**; inspect the resulting diff.

Make targets require Bash and Unix utilities; use a compatible shell on Windows. In `web/`, run `yarn install`, `yarn dev`, and `yarn build` for dependencies, development, and production builds. CI uses Node.js 18. `npm test` runs the two configured utility suites. `yarn lint` and `yarn format` modify files.

## Coding Style & Naming Conventions

Python uses four spaces, snake_case functions/modules, PascalCase classes, and Ruff with an 88-character limit and double quotes. Frontend Prettier uses two spaces, single quotes, semicolons, and a 120-character limit; ESLint checks TypeScript. Follow surrounding naming and directory conventions.

## Testing Guidelines

Use pytest, pytest-asyncio, and mocks for Python; name files `test_*.py` or `*_test.py`. Run focused regressions with `uv run pytest <test-path>`. Frontend utility tests use `node:test` and strict assertions. Add tests for behavior changes; document external-service prerequisites. No numeric coverage gate is configured.

## Commit & Pull Request Guidelines

Follow history: `fix(rag): describe the correction`, `docs: clarify setup`, or `chore: update tooling`. Follow `.github/pull_request_template.md`: explain motivation, link issues, list dependencies, provide reproducible test commands/configuration, and include screenshots for UI changes. Update documentation and self-review before submission.

## Security & Configuration

Keep credentials in environment variables or untracked local configuration. Never commit keys, private datasets, or credential-bearing logs. Use `configs/` templates as starting points.
