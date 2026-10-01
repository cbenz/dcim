# AGENTS.md

## Development rules

- The README.md is the entry point for users: keep it in sync with the features.
- Keep this file **short**. Rules live here; rationale, measurements, prior art
  and gotchas live in `docs/` (see the documentation map below).
- Never weaken a safety rule (see `docs/technical-specs.md`, "Safety
  invariants") without replacing it by a stronger one.

## Language

Everything written in this repository is in **English**: code, comments,
docstrings, documentation, CLI output and commit messages. The language used in
the chat does not matter.

## Documentation map

- [docs/functional-specs.md](docs/functional-specs.md) — the *what*: the two
  commands, workflow, selection criteria, verification levels, CLI reference,
  exit codes.
- [docs/technical-specs.md](docs/technical-specs.md) — the *how*: module map,
  **prior art** (what already exists and why this tool is different), transports,
  digest design, safety invariants, measured performance. Read it **before**
  touching `mtp.py`, `transfer.py` or `deletion.py`.
- [docs/troubleshooting.md](docs/troubleshooting.md) — device, MTP, gvfs and
  CIFS problems met in practice. Read it before "fixing" a mount issue.

## Command surface

Two sub-commands, and that is deliberate:

- `dcim status` (alias `st`) is **read-only**: it may mount and
  read, never create, never write, never delete.
- `dcim sync` copies and verifies. `dcim sync --delete` additionally deletes the
  verified originals.

Rules that follow:

- a new read-only feature goes into `status`, not into a flag of `sync`;
- one alias per command at most, and only when it was asked for: `status`/`st`.
  Inventing aliases multiplies the command surface for nothing;
- **no setting is ever prompted**: a value comes from a flag, the configuration
  file, or a default, and is printed. Prompts are reserved for decisions (the
  copy, the deletion) and for `--prompt`, which is a mode, not a default;
- `--delete` is never read from the configuration file, and no new destructive
  behaviour may be added to `sync` without a flag of its own;
- `analysis.py` stays pure (no I/O, no mount, no terminal) so that it can be
  tested without a device;
- a `partial` digest reads **two blocks, never the file**: reading everything to
  keep two blocks of it is the one mistake that made verification unusably slow
  over a network share. A test pins the dispatch;
- the copy stays **incremental and in two phases** (`Verifying`, then `Copying`) :
  a file already at the destination is never transferred again, but it is still
  verified, because that verification is what allows its original to be deleted.
  A progress bar must never describe work it is not doing;
- the device-side shell is **toybox**, not GNU: `tail -c 0` returns the whole
  file there. Every `-c 0` is banned from the generated scripts, and a test pins
  it — the fake `adb` runs GNU userland and cannot catch this class of bug;
- **one transport per run**, chosen at start-up (`--transport`): `mtp` or `adb`,
  never both. `aft-mtp-mount` caches directory listings and does not see what adb
  changes, so mixing them produces a stale, lying view. Every device access goes
  through `transport.DeviceTransport`; no module may reach for a mount point or
  for `adb` on its own.

## Code conventions

- Python 3.11+, `src/` layout, `uv` for everything (`uv run pytest`, `uv run ruff`).
- Type annotations on every function and method signature; no annotation on
  local variables.
- Full names, no abbreviations (`config`, not `cfg`), and no vague names
  (`data`, `item`, `obj`).
- Tests must never require a phone, a NAS or root: `tests/test_cli.py` replaces
  the MTP mount by a temporary directory, and patches `dcim.session.MtpMount`
  (the class the session actually instantiates).

## Commands

```bash
uv run pytest                          # unit + integration tests
uv run ruff check . --fix              # lint
uv run ruff format .                   # format
uv run basedpyright                    # type check
uv run dcim status --no-check-destination    # a real read, writing nothing
```
