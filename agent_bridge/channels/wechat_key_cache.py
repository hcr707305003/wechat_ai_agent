"""Account-bound DPAPI cache. Never fall back to plaintext storage."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from agent_bridge.channels.chatlog_keys import verify_master_key

logger = logging.getLogger(__name__)
MAX_CACHE_BYTES = 64 * 1024
_HEADER = b"ABKC1\0"


def cache_location(account: Path) -> tuple[Path, bytes]:
    identity = str(account.resolve()).casefold().encode("utf-8")
    entropy = hashlib.sha256(b"AgentBridge/chatlog/v1\0" + identity).digest()
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise OSError("LOCALAPPDATA unavailable")
    return Path(root) / "AgentBridge/cache/chatlog-keys" / (
        entropy.hex() + ".dpapi"
    ), entropy


def _protect(data: bytes, entropy: bytes) -> bytes:
    import win32crypt

    # Current Windows user scope; CRYPTPROTECT_UI_FORBIDDEN, not LOCAL_MACHINE.
    return win32crypt.CryptProtectData(
        data, "Agent Bridge chatlog", entropy, None, None, 1
    )


def _unprotect(data: bytes, entropy: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptUnprotectData(data, entropy, None, None, 1)[1]


def load_master_key(account: Path) -> bytes | None:
    try:
        path, entropy = cache_location(account)
        with path.open("rb") as source:
            encrypted = source.read(MAX_CACHE_BYTES + 1)
        if not encrypted or len(encrypted) > MAX_CACHE_BYTES:
            return None
        payload = _unprotect(encrypted, entropy)
        if len(payload) != len(_HEADER) + 32 or not payload.startswith(_HEADER):
            return None
        master = payload[len(_HEADER) :]
        with (account / "db_storage/contact/contact.db").open("rb") as source:
            page = source.read(4096)
        return master if verify_master_key(master, page) else None
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - optional cache, never expose DPAPI payload/errors
        logger.info("微信密钥加密缓存不可用，将重新校验提取")
        return None


def save_master_key(account: Path, master: bytes) -> None:
    temporary = None
    try:
        if len(master) != 32:
            raise ValueError("Invalid master length")
        path, entropy = cache_location(account)
        encrypted = _protect(_HEADER + master, entropy)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(encrypted)
        os.replace(temporary, path)
        temporary = None
    except Exception:  # noqa: BLE001 - optional optimization must not break reading
        logger.warning("微信密钥加密缓存未保存；本次继续运行，下次启动可能需要重新扫描")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
