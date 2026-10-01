# Technical specifications

## Architecture

```
cli.py       argument parsing and dispatch to the two commands (no logic)
├── status.py     dcim status: read-only reporting
├── sync.py       dcim sync: copy, verify, optionally delete
├── session.py    mount lifetime + source resolution, shared by both
├── analysis.py   pure: histogram by month, presence, suggestion of a range
├── config.py     persisted defaults (~/.config/dcim/config.toml) + overrides
├── mtp.py        aft-mtp-mount: mount, volumes, unmount, gvfs release
├── nas.py        CIFS: mount on demand from /etc/fstab, release
├── mounts.py     /proc/self/mounts reader (the single source of truth)
├── dates.py      capture date from the file name, date range parsing
├── selection.py  scan, glob/extension/date filters, preview summary
├── transfer.py   atomic copy, digest while reading, read-back verification
├── deletion.py   revalidation, deletion, deletion report
├── manifest.py   JSON Lines audit trail, --resume reader
├── verify.py     SHA-256 stream digests (partial / full / none)
├── progress.py   ProgressReporter protocol + rich and silent implementations
├── formatting.py byte, size, count, duration and timestamp formatting
├── ui.py         preview/histogram/suggestion rendering, prompts with non-TTY fallbacks
└── errors.py     AndroidDcimSyncError-derived hierarchy → clean CLI messages
```

Two rules keep this readable:

* **`analysis.py` is pure.** The histogram, the presence index and the
  suggestion take a list of `MediaFile` and return frozen dataclasses; no mount,
  no terminal, no I/O beyond reading a destination directory. That is what makes
  the interesting logic unit-testable.
* **`session.py` owns the mounts.** `status` and `sync` both open a
  `PhoneSession`; the `finally` that releases the device and the destination
  exists once, so no command can forget it.

`status` is read-only by construction: it calls `build_destination_index` (which
only reads) and never `mkdir`, never `ManifestWriter`, never `unlink`.

## Prior art

The combination this tool implements was checked against what already exists
before it was written, because "delete after a verified copy" is an old idea:

| Tool | What it does | Why it is not this tool |
| --- | --- | --- |
| **Rapid Photo Downloader** (Python/Qt, active) | Imports from cameras and phones, copy **or move** (only successfully downloaded files are deleted), verifies downloaded and backed-up files, renaming, backup destinations, automation through CLI options | Qt GUI first: no cron, no headless run. Device access through gphoto2/GIO, not `aft-mtp-mount`. Selection model is "everything new since last time", not a date range. No CIFS handling, no histogram |
| **digiKam** | Import tool with *Download & Delete New/Selected/All* | Deletes without a digest check — its own documentation says to check that the files arrived before deleting. GUI |
| **Immich** (mobile app) *Free Up Space* | Deletes local media already backed up | Requires an Immich server, not a folder on a share; runs on the phone |
| **Nextcloud / Google Photos** *free up space* | Same idea, cloud-side | No control over the destination folder, no date range |
| `immich_sync_assistant`, `ncimgupload` | ADB → Immich/Nextcloud, optional phone cleanup | USB debugging and a server destination |
| Camera-card offloaders: **OffloadBuddy**, **offloader**, **proofcat**, **SD Offload**, `thejoltjoker/offload` | Checksum-verified copy then erase of a card | SD cards and desktop OSes (macOS/Windows), no MTP, no date range |
| `aft-mtp-*`, `jmtpfs`, `simple-mtpfs`, `mtp-rs` (`mtp-mount`, `mtp-tui`, `mtpx`), `adbclone`, `adbackup` | Transport | No verify-then-delete workflow |
| `rclone` | Sync engine | No MTP backend ([issue #6175](https://github.com/rclone/rclone/issues/6175)); it can only act on a FUSE mount |

What this tool adds: a headless, scriptable CLI; MTP through `aft-mtp-mount`
rather than `gvfs`; a date-range-driven selection with a **histogram and a
suggested range**; the CIFS destination mounted and released by the tool itself;
and a per-file manifest with a resumable deletion phase.

The name `dcim` was kept over `offload` precisely because `offload` is saturated
in this space (six media-offload projects, plus GPU offloading), while `dcim`
names the standard folder every camera and phone uses.

## Transports

A run uses **one transport from beginning to end**, chosen at start-up by
`--transport` (`auto` by default: adb when an authorized adb device is present,
the mount otherwise). Mixing them is impossible by construction, and would be
wrong anyway: `aft-mtp-mount` caches directory listings and does not notice
changes made outside it — a directory created by adb is invisible over the
mount, and a file deleted by adb is still listed, and answered with an I/O error
when touched, until the mount is remounted.

Both implement the same small protocol (`transport.py`): list a directory, read
sizes, hash files, copy a file, delete a batch, check what survived. Paths are
**relative to the transport root** everywhere in the program
(``DCIM/Camera/PXL_…jpg``), so no other module knows how the device is reached.

Measured on the reference device (Pixel 7, USB 2 port), same files, same
destination:

| Operation | `mtp` (aft-mtp-mount) | `adb` | Ratio |
| --- | --- | --- | --- |
| Reading one 912 MB video | 7.3 MB/s (125 s) | 106 MB/s (8.2 s) | 15× |
| Deleting 500 files | 25.7 s (51 ms/file) | 2.3 s (4.6 ms/file) | 11× |
| Creating 500 files | 25.3 s (50 ms/file) | 4.0 s (8 ms/file) | 6× |
| Listing + 200 `stat` | 0.01 s (cached) | 0.10 s (one shell call) | — |

The whole pipeline (scan, copy of 4.5 MB, verification, batched deletion of three
files, manifest) takes **1.2 s** on adb, and the full `status` over a 1042-file,
24.96 GB gallery takes **1.8 s** including the destination comparison.

### mtp

Ordinary file operations on a FUSE mount: `os.scandir`, `os.stat`, `open`,
`unlink`. Simple, and the only option when USB debugging is off. The mount is
mounted and released by the program; a mount that predates the run is adopted and
left alone, and anything mounted with a foreign file system type is refused
rather than mounted over.

### adb

* **Metadata**: one `stat -c '%s|%Y|%F|%n' -- *` per directory for a listing, and
  `xargs -0 stat` batches for sizes. No `find -printf %y` (unsupported by toybox)
  and no per-file round trip.
* **Digests are computed on the device**: `head -c H | sha256sum` and
  `tail -c T | sha256sum`, with `H`/`T` derived from the size exactly as
  `verify.compose_partial_digest` does, so the value compares with the locally
  computed one — and **no byte travels over the wire** to verify an
  already-present file. Batches of 32 files share one shell call
  (`SourceDigests`), which is why the "already present" path is fast too.
* **A null byte count is never asked for.** `tail -c 0 file` prints the *whole
  file* on Android (toybox) where GNU tail prints nothing: a file shorter than one
  block would have been hashed twice by the device and would have failed its
  verification — every photo under 1 MiB, silently. The empty digest is inlined
  instead, and a test pins it (the fake `adb`, running GNU userland, could not
  have caught this one).
* **Copy**: `adb pull` (the fast sync protocol; `adb exec-out cat` was measured at
  26 MB/s, four times slower), pulling onto the `.part` file. `adb pull` reports
  its own progress, which would fight with ours, so the local file is polled
  instead. The digest is not free here: it comes from the batched device-side
  hash.
* **Deletion**: `xargs -0 rm -f` with a NUL-separated batch of absolute paths on
  stdin — hundreds of files per shell command. The device is then asked which
  paths survived, so "deleted" is never inferred from a command that returned
  zero.
* `adb pull -a` preserves modification times; the plain form does not, which is
  why the transport sets them itself from the listing.

The failure modes are explicit: a missing directory fails the listing
(`|| exit 66`) instead of looking empty, a size mismatch after a pull is an error,
and a file that survives a deletion is reported as a failure.

## Capture date resolution

The order is: file name, then modification time.

Camera applications put a timestamp in the file name
(`PXL_20250801_163922349.MP.jpg`, `VID_20260102_101112.mp4`), and that timestamp
survives both an MTP transfer and a file system that rewrites `mtime`. Patterns
accepted, in order:

| Pattern | Example |
| --- | --- |
| `YYYYMMDD[-_T ]HHMMSS` | `PXL_20250801_163922349.MP.jpg` |
| `YYYY-MM-DD[-_T ]HH[.:-]MM[.:-]SS` | `IMG-2025-09-30-12-00-00.jpg` |
| `YYYY-MM-DD` | `holiday-2025-09-30.jpg` |

Each candidate is validated with `datetime(...)`: `IMG_20251345_101112.jpg` is
not a date and falls through to the modification time, which is converted with
`datetime.fromtimestamp` (local time, like the device's clock). Both `status` and
`sync` report how many files were dated each way, so a systematically wrong
source is visible before anything is copied.

## Histogram and suggestion (`analysis.py`)

`destination_readiness` decides whether the destination may be read at all,
before any walk: `--no-check-destination` switches the comparison off, a missing
directory is reported as such, and a directory that is a mount point of an
**unmounted** share is refused — an empty mount point would otherwise claim that
nothing is copied. The comparison is a bonus, never a prerequisite.

`build_histogram` groups candidates by `YYYY-MM` and, given an optional
name-to-size index of the destination, counts how many files of each month are
already copied there. The table shows that count as a **share** (`render_copied`:
a small bar plus a percentage), never as a check mark, and only lists months that
hold files. A file counts as copied when **both** its name and its size
match: a same-named file with a different size is reported as missing, which is
exactly what `sync` will report as a conflict.

`build_destination_index` returns a `DestinationIndex` (name → set of sizes seen,
plus counters) and is built to be cheap on a network share:

* **only the wanted names are `stat`ed.** The histogram first asks the device
  what it holds, then asks the destination about *those* 1 000 names instead of
  indexing 45 000 files;
* **the walk is bounded** by `--destination-depth` (0 by default: the destination
  root only), because the destination is often a whole photo library. Measured on
  the reference NAS: 0.41 s for the recommended flat destination (3 169 files,
  all matched) against 7.36 s for the whole `Photos` tree, whose 44 787 entries
  would previously have cost one `stat` each — the reason the command looked hung;
* a name maps to **every** size seen for it, since the same file name can exist
  in several albums and only one of them may be truncated. Presence means "a file
  with this name and this size exists below the destination root";
* dot-directories (`.dcim`, trash, thumbnails) are skipped, unreadable
  directories are counted instead of raising, and the counters are printed by
  `status` so the user can see what was actually read.

`read_storage_space` reads the capacity, the used and the free space of the
device through `statvfs` on the MTP mount. `aft-mtp-mount` does not always
expose a usable `statfs`, so `--phone-capacity` states the total once; it *wins*
over what the device reports (a device reporting nonsense must not be believed),
and the free space is then kept only when it is consistent with that capacity,
otherwise `used` and `free` stay unknown and the report says so. The
`capacity_bar` segments (`to free`, `kept`, `other used`, `free`) are computed
from cumulative boundaries rather than independent ratios, so they always sum to
the requested width whatever the scales involved.

`suggest_range` implements one rule with two modifiers:

* candidates are the months of the histogram, minus the current (incomplete)
  month;
* `keep_months` removes that many months from the recent end;
* `target_size` keeps only the oldest prefix whose cumulative size reaches the
  target, and flags the result as partial when the last month overshoots;
* the result also carries what is *not* copied yet, which is the volume that
  will actually travel.

`kept_months_after` derives the months that stay on the device from the
histogram (everything after the range, plus the current month even when it holds
no file), which is what lets `status` name them instead of counting them, and
lets the histogram draw a row for the month where the range stops.

Sizes are parsed by `parse_size` (`30G`, `1.5TB`, `512MiB`, plain bytes), with
the metric units used by `format_bytes`.

## Integrity: how a copy is proven

The copy and the digest are computed in the same pass:

```
source (MTP) --read chunk--> sha256 head/tail (partial) or whole (full)
                          \-> .part file --> fsync --> atomic rename
                                                        |
                          read back the destination <--/
```

Because the source digest comes from the bytes that were actually read, and the
destination digest from a fresh read of the stored file, a match proves the
chain `device → memory → disk`, not merely the source content. This matters on
FUSE and on CIFS, where a write can be silently short or the connection can drop
mid-file.

A partial digest **reads two blocks and nothing else**, on both sides: reading
the whole file to keep two blocks of it cost 48 s instead of 7.5 s for 32 files
(one of them 90 MB) over CIFS, and made the `Verifying` phase of a 154-file range
take minutes. A regression test pins the dispatch.

Partial digest layout: `sha256(first MiB)` + `"-"` + `sha256(last MiB)`. Two
64-character hex strings cannot be ambiguous. A file of at most two blocks is
covered entirely, and its coverage is recorded as `full`, so a truncated copy of
a small file can never match. Files in the middle are out of scope by
construction (see `tests/test_verify.py::test_partial_digest_ignores_a_middle_corruption`,
which documents the limit and shows `--verify full` catching it).

Failure handling is intentionally conservative:

* the copy is written to `<name>.part-<pid>`, so a crash never leaves a
  half-written file under a usable name;
* a copy that fails verification is deleted at once — an unverified file under a
  real name is worse than no file at all;
* the original is never touched during the copy phase.

## Deletion safety

The deletion phase consumes only `TransferOutcome` objects with
`verified is True`. It revalidates each one immediately before deleting: the
copy exists, its size is the recorded one, its digest still matches the one
recorded during the transfer, and the original still has the size seen during
the scan. Failing any check leaves the original in place, with the reason
recorded. `--resume` rebuilds the very same structure from the manifest.

## Safety invariants

These must hold in every change:

1. **A copy, never a move.** No code path opens an original for writing, or
   renames it.
2. **Verify before delete.** The only input of the deletion phase is the outcome
   of a verification performed in the same run, or replayed from the manifest,
   which recorded it.
3. **Deletion is explicit.** It happens only with `--delete`, only after a
   question that states the number of files and the volume, and whose default
   answer is *no*. Without a terminal, nothing is deleted unless `--yes` is
   given. `--delete` is never read from the configuration file, so it cannot be
   switched on by accident once and forgotten.
4. **`status` and `--dry-run` write nothing** — not even the destination
   directory — and `status` mounts nothing: it reads an already-available
   destination and reports any other situation in one line.
5. **The destination cannot overlap the source.** `check_destination_is_separate`
   refuses a destination inside the device mount (and vice versa), because a
   deletion phase would otherwise destroy the only copy.
6. **Mounts are released in a single place** (`PhoneSession.release`), whatever
   happened before, and a mount that predates the run is never released.
7. **Everything destructive is recorded** in the manifest before it is reported.

## Performance, measured on the target setup

Pixel 7 (internal storage, `DCIM/Camera`) → NAS over CIFS, `aft-mtp-mount`:

| Operation | Measured |
| --- | --- |
| Reading the metadata of 1168 files (`stat`, mtime, size) | 0.07 s |
| Reading `DCIM/Camera` (1168 files, 24.77 GB) | ≈ 6.8 MB/s, so ≈ 1 h |
| Cost of the partial digest | 1 MiB read twice, in a stream already being read |
| `status` destination comparison, flat `--dest` (3 169 files, CIFS) | 0.41 s, only the wanted names `stat`ed |
| `Verifying` 154 already-copied files (2 blocks each, CIFS at 11.5 MB/s) | 61 s, of which ~27 s of reads and ~15 s of device-side hashing |
| `status` destination comparison, whole `Photos` tree (44 787 entries) | 7.36 s, one traversal, still only the wanted names `stat`ed |

The scan is dominated by `readdir`/`stat` over MTP and stays below a second, so
the histogram is instant; the copy is dominated by MTP throughput, which is why
the progress display, the resumable manifest and the "already present" shortcut
matter more than micro-optimisations. The "already present" path costs 2 MiB of
MTP traffic per file instead of a full transfer — the difference between a
one-minute re-run and an hour.

## Configuration file

`~/.config/dcim/config.toml`, written by `--save-defaults`, read at every run.
One flat key per setting, mirroring the `Config` dataclass; unknown keys are an
error rather than a silent typo. The path can be overridden with `--config` or
`DCIM_CONFIG`.

## Machine-readable output

`dcim status --json` prints one JSON document on stdout and sends everything
human (the mount notices, the scan notice) to stderr, so that
`dcim status --json | jq` works. The document holds the totals, the per-month
statistics (`file_count`, `total_size`, `present_count`, `present_size`) and the
suggestion when there is one.

## Testing strategy

* No test needs a phone, a NAS or root.
* `tests/test_cli.py` replaces `MtpMount` with a class whose `volumes()` returns
  a temporary directory: histogram, suggestion, preview, copy, verification,
  deletion, manifest and `--resume` all run for real, against a temporary
  destination, for both commands.
* `tests/test_analysis.py` covers the pure logic: monthly aggregation, presence
  with a size mismatch, every rule of `suggest_range`, size parsing.
* `tests/test_transfer.py` and `tests/test_deletion.py` cover the failure paths
  one by one: truncated copy, tampered copy, filtered copy, changed original,
  missing copy, conflict policies, permission error.
* `tests/test_verify.py` pins the digest contract, including the documented
  limit of the partial mode.
* `tests/test_ui.py` renders against a console capturing its output: the red `▸`
  markers, the two extra rules that delimit the suggested range, the empty row
  drawn for a month that stays without any file, the segments of the capacity bar
  and the wording of the space report (including the two degraded cases: capacity
  without free space, and no capacity at all).
* The suggestion depends on the current month, so `date.today()` is frozen in
  the tests (a run in the first days of a month must not change the expectations).
* Nothing is prompted by default, so the two commands are scriptable as they
  are; when `--prompt` is used, the questions fall back on their defaults without
  a terminal (`ui.is_interactive`), and a directory that does not exist raises
  instead of looping.

## Known limitations

* MTP is slow and single-threaded: throughput is bounded by the device, not by
  this program. Parallelising MTP reads is not supported by
  android-file-transfer and risks corrupting the session.
* A very large directory (tens of thousands of files) is scanned in one pass and
  kept in memory; fine for the 1168 files of a phone camera, not for a media
  library of millions.
* The date range uses the device's local clock for `mtime` and the file-name
  timestamp verbatim; files whose name lies are only visible in the preview and
  in the histogram, where the per-file source is displayed.
* The destination presence check matches on **name and size**. A file copied
  into a sub-directory by hand (with `--preserve-tree`) is therefore reported as
  missing by `status`, which scans the destination recursively but matches by
  name only.
* `aft-mtp-cli` is used only for diagnostics (`--list-devices`); the transfer
  path relies on the FUSE mount.
