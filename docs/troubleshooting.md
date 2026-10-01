# Troubleshooting

Symptoms met on the reference setup (Arch Linux, Pixel 7, NAS over CIFS), with
the cause and the fix.

A good first reflex is `dcim status`: it mounts the device, reads it and prints
what it sees, without writing anything.

## The device is not found

```
Error: aft-mtp-mount exited with code 1:
connect failed: no MTP device found
No MTP device is visible: plug the phone, unlock it and choose the 'file transfer' USB mode
```

Check, in order:

1. `lsusb | grep -i google` — if the device is absent, the cable or the port is
   the problem, not the software. A charge-only cable gives exactly this.
2. On the device, unlock it and choose **File transfer / Android Auto** (not
   *Charging only*). Some phones reset this choice at every reconnection.
3. `dcim --list-devices` — lists what `android-file-transfer` can see, without
   mounting anything.
4. If the device is listed but the mount fails, try `--device <name>` with the
   name printed by `--list-devices`, and `sudo dmesg | tail` to see whether the
   kernel is resetting the port (USB power, hub, cable).

## `--transport adb` says no device is authorized

```
Error: no adb device is authorized: enable USB debugging on the phone...
```

Three things to check, in order:

```bash
adb devices          # "unauthorized" means the phone is waiting for a tap
```

1. On the phone: *Developer options* → **USB debugging** on (and, on MIUI/EMUI,
   "Install via USB" too).
2. Accept the **"Allow USB debugging?"** prompt on the phone screen; if it never
   appears, revoke the authorizations in Developer options and reconnect.
3. `adb kill-server && adb devices` if the server is confused.

`--transport mtp` works without USB debugging, at about a fifteenth of the speed.
`auto` (the default) uses adb when it is available and falls back to the mount
otherwise, saying so.

## The phone is mounted but adb changes are not visible

Expected, and why a run never mixes transports: `aft-mtp-mount` caches directory
listings, so a file created by adb is invisible over the mount, and a file
deleted by adb is still listed (and answers an I/O error when touched). Remount to
resynchronize:

```bash
fusermount3 -uz /run/user/1000/dcim/mtp && aft-mtp-mount /run/user/1000/dcim/mtp
```

If a `dcim` run refuses to unmount with *"Device or resource busy"*, a shell still
has its working directory inside the mount: `cd` out of it, or use
`fusermount3 -uz`.

## The mount fails with "could not claim interface"

`gvfsd-mtp` holds the USB interface. This program stops it before mounting, so
the message means something else got there first — usually a previous run left
behind, or Nautilus reopening the phone.

```bash
pgrep -a gvfsd-mtp          # kill it if it is running
ls /run/user/1000/gvfs      # an mtp: entry means a gvfs mount is still active
mount | grep dcim           # a stale aft-mtp-mount?
```

Unmount a stale mount with `fusermount3 -u <mount point>`, then retry. If
stopping `gvfs` is not acceptable, `--no-release-gvfs` gives up the exclusivity
and lets the mount fail instead of killing the daemon.

## The destination cannot be mounted

```
Error: could not mount /mnt/nas-photos: …
```

The `fstab` entry is `noauto` with a root-only credentials file, so mounting
requires root. The program tries `mount`, then `sudo -n mount`, then
`sudo -n systemctl start <unit>`; without a passwordless `sudo` it cannot do
more, and says so. Two durable fixes:

```bash
# Mount the share on first access, as root, without any password prompt:
sudo systemctl enable --now 'mnt-nas\x2dphotos.automount'

# Or allow the user to mount it directly (the credentials file must then be
# readable by that user):
#   //nas.local/photos /mnt/nas-photos cifs noauto,user,…
```

If the share is already mounted — by systemd or by a previous manual mount — the
program detects it and leaves it mounted at the end of the run.

`dcim status` never mounts it: when the share is absent, it says so in one line
(`/mnt/nas-photos is not mounted (status never mounts it)`) and prints the
histogram and the suggestion anyway. Mount it first (`sudo mount /mnt/nas-photos`)
if you want the "already copied" column, or use `--no-check-destination` to stop
hearing about it.

## `ls` of the device shows a localized directory name

Expected: `aft-mtp-mount` uses the storage label reported by the device
(`Espace de stockage interne partagé` on a French phone, `Internal storage` on an
English one). Nothing is hardcoded: the volume is discovered at runtime and
shown by both commands. A device with an SD card exposes two volumes, and the
program asks which one to use.

## `status` says the free space is not reported

Some devices answer `statfs` over MTP with zeros. State the capacity once, and
the space section shows the source directory as a share of it:

```bash
dcim status --phone-capacity 128G --save-defaults
```

## `status` says a month is not copied, but I copied it

First check what it actually read: `status` prints the destination it inspected
and how much of it (`0 files seen, 98 sub-directories`), or a line saying it could
not read it at all. It reads the destination **root only** by default, because the destination is often a whole
photo library. Two fixes:

```bash
# The copies live in sub-directories:
dcim status --destination-depth 2        # or 'all'

# Better: point --dest at the folder that holds them, once and for all:
dcim status --dest "/mnt/nas-photos/Photos/Pixel 7 Christophe/Camera" --save-defaults
```

Then, `status` counts a file as copied only when its **name and its size** match
a file at the destination. Three cases explain a mismatch:

* the file was copied with a different size (a truncated copy): `sync` will
  report it as a conflict, and `status` counts it as missing — which is the
  honest answer;
* the destination holds it under a different name, or in a sub-directory after a
  `--preserve-tree` run: the presence check matches names, not paths;
* the file was renamed at the destination by another tool.

## A file is skipped as "already present" but I do not trust it

An already present file is only accepted when its size **and** its digest match
the original, so a truncated or different file is never silently accepted — it
is reported as a conflict instead. Re-run with `--verify full` to compare whole
files, or with `--on-conflict rename` to keep both copies.

## The copy is very slow

MTP throughput is the limit (≈ 6.8 MB/s on the reference setup, i.e. about an
hour for 25 GB). Nothing in this program can change that. What helps:

* run `dcim sync` once, then re-run the same range with `--delete`: everything
  already copied is recognized in a few seconds and only the deletion is left;
* use `dcim status` first: it tells you how much of the range is already at the
  destination, so you know what the run will actually transfer;
* keep the device awake and unlocked during the run; a sleeping phone drops the
  connection;
* prefer a USB 3 port and the original cable (the device enumerated as USB 2 in
  some ports).

## The run was interrupted

Nothing was deleted unless `--delete` was given and the deletion phase had
started. Recover in two steps:

```bash
# 1. Where am I? The manifest records everything, including what was copied and
#    what was verified.
ls /mnt/nas-photos/Photos/<phone>/Camera/.dcim/

# 2. Replay the deletion phase for the files that were verified before the crash.
dcim sync --resume <manifest.jsonl>
```

`--resume` re-checks the copies before deleting them, so a copy that was damaged
in the meantime is kept, and the original is kept with it.

## A file was not deleted and the reason is unclear

The deletion phase prints, for every file it keeps, one of these reasons:

| Reason | Meaning |
| --- | --- |
| the copy … is not readable anymore | The destination disappeared or is not mounted |
| the copy … is X instead of Y | The copy has a different size than the original |
| the copy … no longer matches the digest recorded during the transfer | The copy changed since it was verified |
| the original changed since the scan | The device rewrote the file after the scan: it is left alone |

The safest answer is always to re-run; nothing is ever deleted from a copy that
cannot be verified.

## `Syncthing` and similar folders

The phone's `DCIM/Camera` sometimes contains `.stfolder`, `.stversions` and
`thumbnails` — the state directories of Syncthing and of the gallery. They are
excluded by default (`--exclude` to change the list) and, being directories of
small files, they would otherwise dominate the scan.

Deleting a file that Syncthing is tracking propagates the deletion to the other
devices of the mesh. If that is a problem, exclude the camera folder from
Syncthing on the phone, or sync without `--delete` and let the phone's gallery do
the cleanup.
