from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path

import pytest

from agent_bridge.tools.wechat_hook_probe import (
    SUPPORTED_WECHAT_411255,
    WeChatBuildProbeError,
    matches_supported_build,
    probe_wechat_build,
    read_pe_identity,
)


def write_pe(
    path: Path,
    *,
    machine: int = 0x8664,
    timestamp: int = 123,
    image_size: int = 456,
) -> None:
    pe_offset = 0x80
    data = bytearray(0x200)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into(
        "<HHIIIHH", data, pe_offset + 4, machine, 1, timestamp, 0, 0, 0xF0, 0
    )
    struct.pack_into("<H", data, pe_offset + 24, 0x20B)
    struct.pack_into("<I", data, pe_offset + 24 + 56, image_size)
    path.write_bytes(data)


def test_read_pe_identity_reads_stable_header_fields(tmp_path: Path) -> None:
    binary = tmp_path / "Weixin.dll"
    write_pe(binary, timestamp=0x12345678, image_size=0xABC000)

    identity = read_pe_identity(binary)

    assert identity.architecture == "x64"
    assert identity.timestamp == 0x12345678
    assert identity.image_size == 0xABC000


def test_probe_fingerprints_both_files(tmp_path: Path) -> None:
    executable = tmp_path / "Weixin.exe"
    module = tmp_path / "Weixin.dll"
    write_pe(executable, timestamp=1, image_size=2)
    write_pe(module, timestamp=3, image_size=4)

    result = probe_wechat_build(
        executable,
        module,
        version_reader=lambda _path: "4.1.12.55",
    )

    assert result.executable.version == "4.1.12.55"
    assert result.executable.size == 0x200
    assert result.executable.pe.timestamp == 1
    assert result.module.pe.timestamp == 3
    assert len(result.module.sha256) == 64


def test_supported_build_requires_every_field_to_match() -> None:
    assert matches_supported_build(SUPPORTED_WECHAT_411255) is True

    wrong_module = replace(
        SUPPORTED_WECHAT_411255.module,
        pe=replace(
            SUPPORTED_WECHAT_411255.module.pe,
            image_size=SUPPORTED_WECHAT_411255.module.pe.image_size + 1,
        ),
    )

    assert (
        matches_supported_build(
            replace(SUPPORTED_WECHAT_411255, module=wrong_module)
        )
        is False
    )


@pytest.mark.parametrize("content", [b"", b"not-pe", b"MZ" + bytes(100)])
def test_read_pe_identity_rejects_invalid_files(
    tmp_path: Path, content: bytes
) -> None:
    binary = tmp_path / "broken.exe"
    binary.write_bytes(content)

    with pytest.raises(WeChatBuildProbeError):
        read_pe_identity(binary)


def test_probe_rejects_missing_module(tmp_path: Path) -> None:
    executable = tmp_path / "Weixin.exe"
    write_pe(executable)

    with pytest.raises(WeChatBuildProbeError, match="File is unavailable"):
        probe_wechat_build(
            executable,
            tmp_path / "missing.dll",
            version_reader=lambda _path: "4.1.12.55",
        )
