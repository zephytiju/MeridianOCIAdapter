<!-- SPDX-License-Identifier: Apache-2.0 -->

# Contributing

Changes must preserve the one-repository/one-distribution boundary, Apache-2.0
licensing, the released Meridian Object Common contract, the stable
`oci-distribution` Adapter id, and the Platform/Vangu IaC authority boundary.
OCI SDK or physical registry types must not appear in consumer results.

```console
uv sync --frozen --all-extras
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy src
uv run pytest -m 'not integration'
uv run bandit -c pyproject.toml -r src
uv run python scripts/verify_contracts.py
uv build
uv run python scripts/verify_artifacts.py dist
```

Behavior changes require tests at the lowest useful layer and genuine registry
evidence when they depend on provider behavior. Design or public-interface
changes require an approved design write-back before implementation.

Commits should be focused and signed off when required by the contributor's
organization. Pull requests must be green and reviewable without access to
private infrastructure or sibling repository source.
