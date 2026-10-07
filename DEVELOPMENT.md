# Development notes

For contributors: how the package is laid out, how to run the tests, and how releases work. Users only need
the [README](README.md).

## Layout

| Path | What's in it |
| --- | --- |
| `meshive/_client.py` | `Meshive` and `AsyncMeshive`. They share request building, retries and response parsing; only the HTTP transport differs. |
| `meshive/_write.py` | Request builders and argument validation for write calls, shared by both clients. |
| `meshive/models.py` | Response dataclasses. `from_dict` reads the server's camelCase JSON, and every model keeps the full payload on `.raw`. |
| `meshive/exceptions.py` | `MeshiveError` and its subclasses, one per kind of HTTP error. |
| `meshive/formatting.py` | `format_hourly` / `format_usd`: amounts rounded the same way the web console rounds them. |
| `meshive/_config.py`, `meshive/_credentials.py` | API key and base URL resolution, and the credentials file written by `meshive login`. |
| `meshive/cli/` | The `meshive` command: `main.py` (parser and read commands), `_write.py` (write commands), `_format.py` (tables, colors, units). |
| `scripts/release-notes.sh` | Prints one version's section of `CHANGELOG.md` for the GitHub Release. |

## Setup and tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

The tests use `httpx.MockTransport` and a fake client, so they need no network and no API key. CI runs them on
Python 3.10 to 3.13 and runs `pip-audit`.

## Conventions

- Everything in this repository is written in English (see [CLAUDE.md](CLAUDE.md)).
- **Tolerant parsing.** A new response field gets a default in `from_dict`, so the SDK keeps working against
  servers that don't send it yet. A value the server didn't send is `None`, never a made-up `False` or `0`.
- **Validate before sending.** Bad arguments raise `ValueError` before any request goes out.
- **Writes are safe to retry.** Every write sends an `Idempotency-Key`, and automatic retries reuse it.
- **Amounts and units match the console.** Use `formatting` for money. Sizes are 1024-based (GiB, MiB); GPU VRAM
  keeps the usual GB label.
- **CLI writes confirm first.** A command that spends credit or deletes something shows an estimate or a summary and
  asks, unless `--yes` is given. Without a terminal to ask on, it exits with code 2.
- **Server text is cleaned.** The CLI strips control characters from server-provided strings before printing them.

## Compatibility with the API

The SDK talks to the Meshive `/v1/sdk` API. When a server is older than the SDK and lacks a route, the client turns
the bare 404 or 405 into a `NotFoundError` that says the server doesn't support that feature yet, instead of a
confusing "not found".

## Branches and releases

`dev` is the default branch, and pull requests go there. `real` holds what has been released.

To release version `X.Y.Z`:

1. Set `__version__` in `meshive/_version.py`.
2. Add a `## vX.Y.Z` section to `CHANGELOG.md`, written for users.
3. Merge into `real` and push the tag `vX.Y.Z`.

The Publish workflow checks that the tag matches `_version.py`, builds the package, publishes it to PyPI and creates
the GitHub Release with that `CHANGELOG.md` section as its notes. It stops if the section is missing.

Release candidates (`X.Y.ZrcN`) are tagged from `dev`; users get them only with `pip install --pre`.

The [MCP server](https://github.com/meshive/meshive-mcp) depends on a version range of this package, and its
production image only builds once that version is on PyPI, so release the SDK first.
