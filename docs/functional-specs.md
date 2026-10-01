# Functional specifications

## Purpose

Free space on an MTP device — a phone's `DCIM` folder in the common case, but any
MTP device works — by copying photos and videos to a destination such as a NAS
share, verifying every copy, and deleting the originals from the device only
once they are proven intact.

The selection is driven by a **capture date range** (`2025-09 .. 2026-08`) and
not by file size: a run targets a known period, so what leaves the device is
predictable and auditable. A second command, `status`, answers "what is on the
device, and what should go next?" before anything is copied.

## Two commands, one rule

| Command | Writes? | Deletes? | Purpose |
| --- | --- | --- | --- |
| `status` (alias `st`) | no | no | Histogram of the device by month, comparison with the destination, suggested range |
| `sync` | yes | no | Copy a range and verify it (a backup) |
| `sync --delete` | yes | yes | Copy, verify, then delete the verified originals (an offload) |

These are the only two names: no extra aliases.

`status` never creates the destination directory, never writes a manifest, and
never touches the device. `sync` never deletes unless `--delete` was given on
that specific run, and `--delete` cannot be persisted in a configuration file.

## Actors and prerequisites

| Actor | Role |
| --- | --- |
| The user | Chooses the source directory, the glob pattern and the date range; confirms the copy, then the deletion |
| The device | Exposes its storage over MTP, mounted with `aft-mtp-mount` |
| The destination | Exposes the share over CIFS, mounted from the `/etc/fstab` entry |
| This program | Mounts both, scans, copies, verifies, deletes, and keeps a manifest |

The device must be plugged in, unlocked, and set to *File transfer* / MTP mode.

## Session start-up (both commands)

Each step happens in this order, and every prompt has a non-interactive
equivalent (a command line flag, or a value saved in the configuration file).

1. **Destination.** For `sync`, the CIFS mount point (default
   `/mnt/nas-photos`) is mounted if it is not already mounted. A mount that
   already existed is left alone at the end of the run; one this program mounted
   is released. **`status` never mounts anything**: it reads the destination only
   when it is already available, and reports the situation when it is not.
2. **Device.** A transport is chosen once, for the whole run, and never changes:
   * `--transport adb` — or `auto` when USB debugging is on: the device is reached
     with `adb shell` (listing, metadata, deletion) and `adb pull` (bytes).
     Measured at 106 MB/s reading and ~4.6 ms per deletion.
   * `--transport mtp` — or `auto` when adb is unavailable: `gvfsd-mtp` is stopped
     if it is running (the MTP USB interface can only be claimed by one process),
     and `aft-mtp-mount` mounts the device on a private mount point
     (`$XDG_RUNTIME_DIR/dcim/mtp`). Measured at 7.3 MB/s and ~51 ms per deletion.

   The storage volumes exposed by the device are listed; when there is more than
   one (internal storage *and* an SD card), the user picks one. A mount that
   predates the run is adopted and left in place.
3. **Source directory**, relative to the storage volume, e.g. `DCIM/Camera`. The
   available directories are listed to help.
4. **Glob pattern**, matched against the file names; a pattern containing `/` is
   matched against the path relative to the source directory. Several patterns
   can be separated by commas: `*.jpg,*.mp4`. A second, implicit filter keeps
   only known photo and video extensions unless `--all-files` is given. The
   pattern defines the file set of the run: the histogram of `status` and the
   files `sync` copies are the same set, so a range suggested by one is
   meaningful for the other.

**Nothing is asked.** The source directory, the glob pattern and the date range
are resolved from, in order: the command line flag, the configuration file, the
built-in default (last complete months). Both commands then **print** what they
resolved, before doing anything:

```
Source: /run/user/1000/dcim/mtp/…/DCIM/Camera
Destination: /mnt/nas-photos/Photos/Pixel 7 Christophe/Camera
Pattern: * — date range 2025-08-01 .. 2025-10-01 (end excluded)
```

What remains interactive is not a setting but a decision: the confirmation of the
copy, and the confirmation of the deletion. `--yes` answers both in advance, for
scripts; `--prompt` restores the guided questions (volume, source directory,
glob pattern, date range) for exploring another directory by hand.

## `dcim status`

Read-only reporting, in three parts.

### What it reads

`status` scans the source directory (the device side), then — if and only if the
destination is already available — reads it to tell what is already copied.
Three situations, and only the first is a comparison:

| Destination state | What `status` does |
| --- | --- |
| mounted and readable | compares, prints the `Copied` column and what is left to transfer |
| missing, or a share that is not mounted | says so in one line, and prints the histogram and the suggestion without it |
| `--no-check-destination` | same, on request (persistable with `--save-defaults`) |

A destination problem therefore never prevents the answer to "what is on the
phone, and what should I sync next" — reading the device is the only dependency
`status` has. The destination walk is **bounded on purpose**: it reads the destination root only by default, because the
destination is often a whole photo library — comparing 1 000 phone files against
a 45 000-file tree means nothing useful and costs a lot on a network share.
`--destination-depth N` widens the walk (`--destination-depth all` reads
everything). Only the file names seen on the device are ever `stat`ed, so a deep
walk costs a directory traversal, not one `stat` per file of the library.

When nothing matches at the destination root, `status` says so and points at the
two fixes: a deeper `--destination-depth`, or a `--dest` that holds the copies.

### Space

The section that answers "how much do I get back", in four lines and a bar:

| Line | Meaning |
| --- | --- |
| `Phone storage` | total capacity, used (and its share), free |
| `<source>` | what the source directory takes, its share of what is used, its file count |
| `At destination` | what is left to transfer, when the destination was read |
| `Would move` | the size the suggested range would move off the device |
| `After the move` | the free space once that range is gone, and what it was |

Then a bar over the capacity, where the part that would be freed is a segment of
its own: `█ to free`, `█ kept`, `█ other used`, `░ already free`, with the legend
giving each number.

The free space comes from the device (`statvfs` over the MTP mount). When the
device does not report it, `--phone-capacity 128G` (persistable) states the total
once, and the section shows the source directory as a share of the capacity,
saying explicitly that the free space is not reported.

### Histogram

One line per month found in the source directory, with:

* the number of files and their total size;
* a proportional bar, so the shape of the archive is visible at a glance;
* the cumulative size, to read "up to here, I would free X";
* **how much of that month is already at the destination**, when the destination
  is inspected: a share, drawn as a small bar plus a percentage (`█████░ 87%`). A
  file counts as copied when a file of the same name **and** the same size exists
  at the destination — the same rule `sync` applies. It is deliberately not a
  check mark: a check mark in a column about copying reads as "all of it is to be
  copied", the opposite of what a complete month means.

The months of the suggested range are **delimited by rules and marked with a red
`▸`**, with a legend under the table naming the range — and mentioning that the
other months stay on the device only when there are such months. Only months that
hold files are listed: a month that does not exist yet has nothing to say.

The current month is shown like the others but is never part of a suggestion: a
month still being written would have to be synced again a few days later.

### Phone storage

When the MTP mount reports a usable `statfs`, the free and total space of the
device are printed — the number that makes the whole exercise meaningful.

### Suggestion

The range to hand to `dcim sync`, plus the exact command. The rule:

* start at the oldest month seen on the device;
* stop before the months the user wants to keep — `--keep-months N` drops that
  many months from the recent end, and the current (incomplete) month is always
  excluded;
* `--target-size 30G` cuts the range at the oldest months until the size to free
  is reached, flagging that the last month is only partly needed.

The suggestion reports what is still to transfer (files not yet at the
destination), which is the volume that will actually travel; the size that will
be *freed* is the total of the range, since files already at the destination are
still deletable after verification. It also **names the months that stay**, with
their file count when they have one and `no file yet` when they do not: "the last
month of the range is kept" is the opposite of what a range ending before the
current month means, and a count invites that misreading.

`--json` prints the same information as a JSON document on stdout (the human
chatter goes to stderr), for scripting.

## `dcim sync`

### Date range

Without `--from`/`--to`, the range is the last `--months` complete months and no
question is asked: the effective range is printed with the source and the pattern
before the preview. The capture date is read from the file name (`PXL_20250801_163922349.MP.jpg` →
2025-08-01 16:39:22) and falls back on the modification time when the name
carries no recognisable timestamp. The range is half-open: `start <= capture date
< end`. `YYYY-MM` covers a whole month, so `--to 2026-08` includes August 2026.
Without arguments, the default window is the last 12 complete months — a run in
September 2026 targets `2025-09 .. 2026-08`.

### Preview

Nothing is copied before the user has seen:

* the number of files and the total size to copy;
* the oldest and the newest capture date of the selection;
* how many dates came from the file name and how many from the modification time;
* a table of file counts and sizes per month, and the share already copied when
  the destination is inspected;
* the source, the destination, the pattern, the date range, and whether the
  originals will be deleted.

The preview deliberately lists **no file names**: twelve near-identical names
teach nothing that the monthly table and the totals do not already say. The
number of files dated from their modification time — the ones that could fall in
the range by accident — is the signal to read twice, and it is printed. Where the
files will land is shown by `--dry-run`.

Then a single confirmation question is asked. `--yes` answers it in advance and
`--dry-run` stops right after the preview, having written nothing at all.

### Copy

The copy is **incremental, in two phases**:

1. **Verifying** — the files the destination already has (same name, same size)
   are not copied. They are still *verified*: their digest is compared with the
   destination, because that comparison is what allows their original to be
   deleted later. On adb the digests are computed on the device, in batches, so
   nothing travels over the wire to check a file that is already there.
2. **Copying** — only what the destination does not have. The progress bar covers
   exactly that, so it starts at 0 % with a real volume and ends at 100 % (or
   short of it when a file failed, which is the honest signal that something went
   wrong).

A reuse run over an already-copied range therefore transfers nothing at all, and
says so: `already_present` in the report and no copy phase.

For each file to copy, in capture date order:

1. a `.part` file is written next to the destination;
2. the source digest is computed **while reading**, from the bytes actually
   transferred;
3. the copy is `fsync`ed and atomically renamed into place;
4. the destination is read back and its digest compared with the source one;
5. the modification time of the original is copied onto the copy;
6. the outcome is appended to the manifest.

A file already present with the same name and the same size is *not* copied
again: its destination digest is compared with the source one, and it counts as
verified when they match. A file already present with a **different** content is
a conflict, reported and skipped by default (`--on-conflict
skip|overwrite|rename`).

A failure never stops the run: it is reported, recorded in the manifest, and the
file is left on the device. In parallel, a two-level progress display shows the
overall progress, the current file, the throughput and an estimated remaining
time.

### Verification levels

| Level | Source side | Destination side | Cost |
| --- | --- | --- | --- |
| `partial` (default) | SHA-256 of the first and last MiB, while copying | read back | 2 MiB per file |
| `full` | SHA-256 of the whole file, while copying | read back | one full read of the copy |
| `none` | — | — | none, size comparison only |

`partial` is the default because the source is hashed during the transfer
itself, so it costs nothing, and because a truncated or partially written copy
is always caught: files shorter than two blocks are covered entirely, and their
digest is stored as `full` coverage so they can never match a partial one.

### Deletion (`--delete`)

Without `--delete`, `sync` stops here and the originals stay on the device.

With `--delete`, the deletion phase only sees the transfers that were **verified
in this run**. Before each deletion, three checks are repeated:

1. the copy still exists and still has the recorded size;
2. the copy still matches the digest recorded during the transfer;
3. the original still has the size recorded during the scan.

The user then confirms the deletion explicitly (the question states the number of
files and the volume involved). **The default answer is yes**: pressing Enter
deletes the verified copies, answering `n` keeps everything. Reaching that
question already required `--delete`, a verified copy of every file counted, and
a terminal — without one, nothing is deleted unless `--yes` is given. Nothing is ever
deleted when a copy cannot be verified, and a failure during deletion is
reported without interrupting the other files. Without a terminal, the deletion
does not happen unless `--yes` is given.

## Manifest and resume

Every `sync` run writes
`<destination>/.dcim/manifest-<timestamp>.jsonl`, one JSON object per event,
flushed on every write:

* `run` — the source volume, the source sub-directory, the pattern, the date
  range, the verification level: everything needed to understand the run;
* `transfer` — one record per file, with its size, its status and its digest;
* `deletion` — one record per file attempted, with the outcome or the reason it
  was kept.

`dcim sync --resume MANIFEST` replays the deletion phase of an interrupted run:
it rebuilds the verified transfers from the manifest, remounts the device, and
asks for the deletion confirmation again. This is the recovery path after a
crash, a disconnection or a `Ctrl+C` during the copy.

## Command line reference

### Both commands

| Flag | Default | Effect |
| --- | --- | --- |
| `--config PATH` | `~/.config/dcim/config.toml` | Configuration file to use |
| `--save-defaults` | off | Persist the answers of this run |
| `--source PATH` | `DCIM/Camera` | Source directory, relative to the storage volume |
| `--pattern GLOBS` | `*` | Glob pattern(s), comma separated |
| `--exclude GLOB` | `.stfolder`, `.stversions`, `thumbnails`, `*.trashed-*` | Patterns to skip (repeatable) |
| `--device NAME` | auto | MTP device name or serial to use |
| `--recursive / --no-recursive` | recursive | Recurse into sub-directories |
| `--media-only / --all-files` | media only | Keep only known photo and video extensions |
| `--dest PATH` | `/mnt/nas-photos/Photos` | Destination directory |
| `--nas-mountpoint PATH` | `/mnt/nas-photos` | CIFS mount point to mount on demand |
| `--transport auto\|adb\|mtp` | `auto` | How to reach the device: `adb` when USB debugging is on, `mtp` through the mount, `auto` to pick |
| `--keep-mounted` | off | Leave the mounts in place at the end |
| `--no-release-gvfs` | off | Do not stop `gvfsd-mtp` before mounting |
| `--quiet` | off | Hide the progress display (the report is still printed) |
| `--prompt / --no-prompt` | off | Ask for the source directory, the glob pattern and the date range |

`--list-devices` and `--version` are handled before any sub-command.

### `status`

| Flag | Default | Effect |
| --- | --- | --- |
| `--keep-months N` | 0 | Months to keep on the device when suggesting a range |
| `--target-size SIZE` | none | Suggest the oldest months until this size is reached, e.g. `30G` |
| `--phone-capacity SIZE` | none | Total size of the device, when its MTP mount does not report it, e.g. `128G` |
| `--check-destination / --no-check-destination` | on | Read the destination (never mounted) to tell what is already copied |
| `--destination-depth N\|all` | 0 | Directory levels read at the destination (0 = the root only) |
| `--json` | off | Print the report as JSON on stdout |

### `sync`

| Flag | Default | Effect |
| --- | --- | --- |
| `--from YYYY-MM[-DD]` | last 12 complete months | Start of the capture date range |
| `--to YYYY-MM[-DD]` | last 12 complete months | End of the capture date range (whole month included) |
| `--months N` | 12 | Size of the default window |
| `--mount-nas / --no-mount-nas` | mount | Mount the destination when it is not mounted |
| `--verify partial\|full\|none` | `partial` | Verification level |
| `--on-conflict skip\|overwrite\|rename` | `skip` | Behaviour when a different file exists |
| `--preserve-tree` | off | Reproduce the source sub-directories under the destination |
| `--no-preserve-times` | keep times | Do not copy the modification times |
| `--limit N` | none | Process only the N oldest matching files |
| `--dry-run` | off | Preview only; copies and deletes nothing |
| `--delete` | off | Delete the verified originals from the device |
| `--yes` | off | Answer every confirmation with yes |
| `--resume MANIFEST` | none | Replay the deletion phase of a run |

Exit codes: `0` everything verified (and deleted, when asked), `1` expected
error (mount failure, unusable source, invalid range), `2` some files were left
behind (failures or conflicts), `130` interrupted by the user.

## Guarantees and non-goals

Guarantees:

* an original is deleted only if its copy was verified in the same run, and only
  when `--delete` was asked for;
* a run interrupted at any point leaves the device untouched and the manifest
  usable;
* `status` and `--dry-run` write absolutely nothing; `status` never asks a
  question (it reports the filter it used) and never mounts the destination;
* one transport per run, from the scan to the deletion: the MTP mount caches
  directory listings and does not notice what adb changes;
* the destination is refused when it overlaps the device mount;
* a mount that existed before the run is never released by the run.

Non-goals:

* syncing back to the device, or between two devices;
* reading the device through `gvfs` (`mtp://` URLs), which is precisely what
  this tool avoids;
* a retention policy ("older than N days"): the date range is explicit;
* touching anything outside the source directory chosen by the user.
