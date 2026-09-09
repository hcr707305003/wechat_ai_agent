from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


class WeChatBuildProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PEIdentity:
    architecture: str
    timestamp: int
    image_size: int


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    version: str
    size: int
    sha256: str
    pe: PEIdentity


@dataclass(frozen=True, slots=True)
class WeChatBuildFingerprint:
    executable: FileFingerprint
    module: FileFingerprint


SUPPORTED_WECHAT_411255 = WeChatBuildFingerprint(
    executable=FileFingerprint(
        version="4.1.12.55",
        size=3_127_848,
        sha256="bb301eb25b9748d471d8a7e5fb142f6e63b4bf2ecc2d39346e73a902eba5c135",
        pe=PEIdentity("x64", 0x6A793C56, 0x305000),
    ),
    module=FileFingerprint(
        version="4.1.12.55",
        size=194_903_080,
        sha256="4d92c4c381a8ceca4c591fea7894298674f89a800a2d3c81d946014aa8524dee",
        pe=PEIdentity("x64", 0x6A793C40, 0xBA56000),
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pe_identity(path: Path) -> PEIdentity:
    try:
        with path.open("rb") as stream:
            dos_header = stream.read(64)
            if len(dos_header) != 64 or dos_header[:2] != b"MZ":
                raise WeChatBuildProbeError(f"Not a PE file: {path}")
            pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
            stream.seek(pe_offset)
            if stream.read(4) != b"PE\0\0":
                raise WeChatBuildProbeError(f"Invalid PE signature: {path}")
            file_header = stream.read(20)
            if len(file_header) != 20:
                raise WeChatBuildProbeError(f"Truncated PE header: {path}")
            machine, _sections, timestamp, _symbols, _symbol_count, optional_size, _flags = (
                struct.unpack("<HHIIIHH", file_header)
            )
            optional_header = stream.read(optional_size)
    except OSError as error:
        raise WeChatBuildProbeError(f"Unable to read PE file: {path}") from error

    if len(optional_header) < 60:
        raise WeChatBuildProbeError(f"Truncated optional PE header: {path}")
    magic = struct.unpack_from("<H", optional_header, 0)[0]
    if magic not in (0x10B, 0x20B):
        raise WeChatBuildProbeError(f"Unsupported PE optional header: {path}")
    architecture = {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}.get(
        machine, f"machine_0x{machine:04x}"
    )
    image_size = struct.unpack_from("<I", optional_header, 56)[0]
    return PEIdentity(architecture, timestamp, image_size)


class _VSFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("signature", ctypes.c_uint32),
        ("struct_version", ctypes.c_uint32),
        ("file_version_ms", ctypes.c_uint32),
        ("file_version_ls", ctypes.c_uint32),
        ("product_version_ms", ctypes.c_uint32),
        ("product_version_ls", ctypes.c_uint32),
        ("file_flags_mask", ctypes.c_uint32),
        ("file_flags", ctypes.c_uint32),
        ("file_os", ctypes.c_uint32),
        ("file_type", ctypes.c_uint32),
        ("file_subtype", ctypes.c_uint32),
        ("file_date_ms", ctypes.c_uint32),
        ("file_date_ls", ctypes.c_uint32),
    ]


def read_file_version(path: Path) -> str:
    try:
        version_api = ctypes.windll.version
    except AttributeError as error:
        raise WeChatBuildProbeError("Windows version API is unavailable") from error

    size = version_api.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise WeChatBuildProbeError(f"File version is unavailable: {path}")
    buffer = ctypes.create_string_buffer(size)
    if not version_api.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise WeChatBuildProbeError(f"Unable to read file version: {path}")
    value = ctypes.c_void_p()
    value_size = ctypes.c_uint()
    if not version_api.VerQueryValueW(
        buffer, "\\", ctypes.byref(value), ctypes.byref(value_size)
    ):
        raise WeChatBuildProbeError(f"Invalid file version resource: {path}")
    if value_size.value < ctypes.sizeof(_VSFixedFileInfo):
        raise WeChatBuildProbeError(f"Truncated file version resource: {path}")
    info = ctypes.cast(value, ctypes.POINTER(_VSFixedFileInfo)).contents
    return ".".join(
        str(part)
        for part in (
            info.file_version_ms >> 16,
            info.file_version_ms & 0xFFFF,
            info.file_version_ls >> 16,
            info.file_version_ls & 0xFFFF,
        )
    )


def fingerprint_file(
    path: Path,
    *,
    version_reader: Callable[[Path], str] = read_file_version,
) -> FileFingerprint:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise WeChatBuildProbeError(f"File is unavailable: {path}") from error
    return FileFingerprint(
        version=version_reader(path),
        size=size,
        sha256=_sha256(path),
        pe=read_pe_identity(path),
    )


def probe_wechat_build(
    executable_path: Path,
    module_path: Path,
    *,
    version_reader: Callable[[Path], str] = read_file_version,
) -> WeChatBuildFingerprint:
    return WeChatBuildFingerprint(
        executable=fingerprint_file(executable_path, version_reader=version_reader),
        module=fingerprint_file(module_path, version_reader=version_reader),
    )


def matches_supported_build(fingerprint: WeChatBuildFingerprint) -> bool:
    return fingerprint == SUPPORTED_WECHAT_411255


def main() -> int:
    parser = argparse.ArgumentParser(description="只读探测微信 Hook 构建指纹")
    parser.add_argument("--exe", type=Path, required=True, help="Weixin.exe 路径")
    parser.add_argument("--module", type=Path, required=True, help="Weixin.dll 路径")
    args = parser.parse_args()
    try:
        fingerprint = probe_wechat_build(args.exe, args.module)
    except WeChatBuildProbeError as error:
        parser.error(str(error))
    output = asdict(fingerprint)
    output["supported"] = matches_supported_build(fingerprint)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["supported"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
