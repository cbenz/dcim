# dcim

Offload photos and videos from an MTP device (phone, camera) to a NAS share,
verifying every copy before the originals leave the device.

```
$ dcim status
── Space
Phone storage    total 118.02 GB · used 117.17 GB (99%) · free 845.15 MB
Camera (1169 files)  26.60 GB — 23% of what is used
Would move       26.60 GB — 1169 files, months 2025-08 .. 2026-09
After the move   free 27.45 GB (was 845.15 MB)

Phone  ██████████████████████████████████  117.17 GB / 118.02 GB used
       █ to free 26.60 GB  █ nothing stays  █ other used 90.57 GB  ░ free 845.15 MB

── Histogram
Files per month
 ───────────────────────────────────────────────────────────────
  Month     Files        Size   Distribution     Cumulative
 ───────────────────────────────────────────────────────────────
  2025-08      98     1.13 GB   ██░░░░░░░░░░░░      1.13 GB   ▸
  2025-09      29   513.52 MB   █░░░░░░░░░░░░░      1.64 GB   ▸
  …
  2026-09      52     1.63 GB   ███░░░░░░░░░░░     26.60 GB   ▸
 ───────────────────────────────────────────────────────────────
  2026-10       0           —                             —
 ───────────────────────────────────────────────────────────────
▸ = months of the suggested range 2025-08 .. 2026-09; the other months stay on the phone

── Suggestion
2025-08 .. 2026-09  1169 files, 26.60 GB would move
  of which 1169 files (26.60 GB) still to transfer
  staying on the phone: 2026-10 (current month, no file yet)

  dcim sync --from 2025-08 --to 2026-09
  dcim sync --from 2025-08 --to 2026-09 --delete   # frees 26.60 GB on the phone
```

Reading it: **what would move** is marked with a red `▸` and delimited by rules,
and the months that stay — the current one, which holds no file yet, gets a row
of its own so that the boundary is visible — are named under the table. The
space section gives the three numbers that matter: what the phone holds, what
the source directory takes, and how much would be free after the move.
Everything comes from the device itself; `--phone-capacity 128G` fills in the
total when the MTP mount does not report it.

## Two commands

| Command | What it does |
| --- | --- |
| `dcim status` (alias `st`) | **Read only**: mounts the phone, reads the capture date of every matching file, prints a histogram by month, compares with the destination, and suggests the range to sync next. Never writes anything, not even the destination directory. |
| `dcim sync` | Copies a date range, verifies every copy against the destination, and keeps the originals. |
| `dcim sync --delete` | The same, then deletes from the phone the originals whose copy was verified — after an explicit confirmation. |

**Nothing is asked.** The source directory, the glob pattern and the date range
come from the flags, then from `~/.config/dcim/config.toml`, then from their
defaults (the last complete months), and are printed before anything happens.
What remains is the two confirmations — the copy, then the deletion — which are
decisions rather than settings; `--prompt` restores the guided questions.

`--delete` is never read from a configuration file either: removing originals is
a decision taken again on every run.

## Why this tool

- **Two transports, measured on the device.** `--transport adb` (USB debugging)
  reaches the phone with `adb shell` for metadata and deletion and `adb pull` for
  the bytes: **106 MB/s** reading and **~4.6 ms per deletion**. `--transport mtp`
  mounts the phone with `aft-mtp-mount` (android-file-transfer) and uses ordinary
  file operations: **7.3 MB/s** and **~51 ms per deletion**. `auto` picks adb when
  it is available. `gvfs` is never used, and the two transports are never mixed —
  the mount caches directory listings and would report a stale view of what adb
  changed.
- **A copy, never a move.** Every file is copied to a `.part` file, `fsync`ed,
  renamed into place, read back and compared. An original is deleted only when
  its copy was verified *in the same run* — so a file whose copy failed is always
  kept, while the others are freed.
- **Incremental.** A file the destination already has (same name, same size) is
  never copied again: it is *verified* instead, and the copy phase covers only
  what is missing — so re-running a range transfers nothing and says so.
- **Driven by capture dates**, read from the file name
  (`PXL_20250801_163922349.MP.jpg`) with the modification time as a fallback —
  not by file size, so a run targets a period you chose.
- **An audit trail.** Every run writes a JSON Lines manifest next to the copies
  (`.dcim/manifest-*.jsonl`), and `--resume` replays the deletion phase of an
  interrupted run.

See [docs/technical-specs.md](docs/technical-specs.md) for the prior art (Rapid
Photo Downloader, digiKam, Immich, the camera-card offloaders) and what this
tool does differently.

## Requirements

- Linux with Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- `android-file-transfer` (`aft-mtp-mount`, `aft-mtp-cli`)
- `cifs-utils` and an `/etc/fstab` entry for the destination share
- The device plugged over USB, unlocked, in *File transfer* / MTP mode

## Install

```bash
git clone <this repository> ~/Dev/perso/dcim
cd ~/Dev/perso/dcim
uv sync
uv run dcim --help
```

## Usage

```bash
# 1. What is on the phone, and what should go next?
dcim status

# 2. Copy that range, and keep the originals (a backup)
dcim sync --from 2025-09 --to 2026-08

# 3. Same range, but free the phone once the copies are verified
dcim sync --from 2025-09 --to 2026-08 --delete

# 4. Save the answers (source, pattern, destination) for the next runs
dcim status --dest "/mnt/nas-photos/Photos/Pixel 7 Christophe/Camera" --save-defaults
```

Useful flags:

| Flag | Commands | Effect |
| --- | --- | --- |
| `--from YYYY-MM` / `--to YYYY-MM` | `sync` | Capture date range, end month included |
| `--months N` | `sync` | Window used when no range is given (default 12) |
| `--keep-months N` | `status` | Months to keep on the phone when suggesting a range |
| `--target-size 30G` | `status` | Suggest the oldest months until this size is reached |
| `--phone-capacity 128G` | `status` | Total size of the device, when its MTP mount does not report it |
| `--check-destination / --no-check-destination` | `status` | Read the destination (never mounted) to tell what is already copied |
| `--destination-depth N\|all` | `status` | Levels read at the destination (default 0: its root only) |
| `--prompt / --no-prompt` | both | Ask for the source directory, the glob pattern and the date range, instead of taking them from the flags and the configuration file |
| `--destination-depth N\|all` | `status` | Directory levels read at the destination (0 = the root only, the default) |
| `--prompt / --no-prompt` | both | Ask for the source directory, the glob pattern and the date range, instead of taking them from the flags and the configuration file |
| `--json` | `status` | Machine-readable report on stdout |
| `--dry-run` | `sync` | Show the plan, copy and delete nothing |
| `--limit N` | `sync` | Process only the N oldest matching files (good first try) |
| `--verify partial\|full\|none` | `sync` | Verification level (default `partial`) |
| `--on-conflict skip\|overwrite\|rename` | `sync` | Behaviour for an existing different file (default `skip`) |
| `--mount-nas / --no-mount-nas` | `sync` | Mount the destination when it is not mounted |
| `--transport auto\|adb\|mtp` | both | How to reach the phone (`auto` = adb when USB debugging is on) |
| `--resume MANIFEST` | `sync` | Replay the deletion phase of an interrupted run |
| `--yes` | `sync` | Do not ask for confirmation |
| `--list-devices` | — | List the MTP devices android-file-transfer can see |

Exit codes: `0` everything verified and deleted, `1` expected error, `2` some
files were left behind (failures or conflicts), `130` interrupted.

## Documentation

- [docs/functional-specs.md](docs/functional-specs.md) — what each command does, step by step
- [docs/technical-specs.md](docs/technical-specs.md) — how it is built, why, and the prior art
- [docs/troubleshooting.md](docs/troubleshooting.md) — phone, MTP, gvfs and CIFS problems
- [AGENTS.md](AGENTS.md) — rules for contributors (and coding agents)

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run basedpyright
```
