"""Tests for the configuration file and the mount helpers."""

from pathlib import Path

import pytest

from dcim.config import (
    Config,
    config_from_mapping,
    load_config,
    render_toml,
    save_config,
)
from dcim.errors import AndroidDcimSyncError
from dcim.mounts import _unescape, filesystem_type, is_mounted
from dcim.nas import systemd_unit_name


def test_defaults_are_used_when_the_file_does_not_exist(tmp_path: Path):
    config = load_config(tmp_path / "missing.toml")
    assert config.destination_root == Config().destination_root
    assert config.verify_mode == "partial"


def test_save_and_load_round_trip(tmp_path: Path):
    config = Config(
        destination_root=Path("/mnt/nas-photos/Photos/Pixel 7"),
        source_directory="DCIM/Camera",
        file_pattern="*.jpg,*.mp4",
        media_only=False,
        recursive=False,
        months=6,
        verify_mode="full",
        conflict_policy="rename",
        exclude_patterns=("thumbnails", ".stversions"),
        device_name="Google-Pixel7",
    )
    path = save_config(config, tmp_path / "config.toml")
    assert load_config(path) == config


def test_rendered_toml_is_stable(tmp_path: Path):
    rendered = render_toml(Config())
    assert 'verify_mode = "partial"' in rendered
    assert "media_only = true" in rendered
    assert "exclude_patterns =" in rendered


def test_unknown_keys_are_rejected():
    with pytest.raises(AndroidDcimSyncError, match="unknown configuration keys: nope"):
        config_from_mapping({"nope": 1})


def test_invalid_toml_is_reported(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("verify_mode = \n")
    with pytest.raises(AndroidDcimSyncError, match="not valid TOML"):
        load_config(path)


def test_unescape_handles_spaces_in_mount_points():
    assert _unescape("/media/My\\040Phone") == "/media/My Phone"


def test_the_root_file_system_is_always_mounted():
    assert is_mounted(Path("/"))
    assert filesystem_type(Path("/")) is not None


def test_an_arbitrary_directory_is_not_a_mount_point(tmp_path: Path):
    assert not is_mounted(tmp_path)


@pytest.mark.parametrize(
    ("mountpoint", "expected"),
    [
        (Path("/mnt/nas-photos"), "mnt-nas\\x2dphotos.mount"),
        (Path("/mnt/nas-photos"), "mnt-nas\\x2dphotos.automount"),
    ],
)
def test_systemd_unit_names(mountpoint: Path, expected: str):
    kind = "automount" if expected.endswith("automount") else "mount"
    assert systemd_unit_name(mountpoint, kind) == expected
