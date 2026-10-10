"""Use chatlog key recovery while preserving the existing database API."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from agent_bridge.channels.chatlog_keys import recover_master_key
from agent_bridge.channels.wechat_key_cache import load_master_key, save_master_key

logger = logging.getLogger(__name__)


def _database_module():
    from wechatauto import db

    return db


def select_account(candidates, account: str | None) -> Path:
    directories = [
        Path(path) for path in candidates if (Path(path) / "db_storage").is_dir()
    ]
    if account:
        exact = [path for path in directories if path.name == account]
        directories = exact or [
            path
            for path in directories
            if re.fullmatch(re.escape(account) + r"_\w{4}", path.name)
        ]
    if len(directories) != 1:
        raise RuntimeError(
            "无法唯一确定微信账号，请在 channels.wechat.account 中指定完整账号目录名"
        )
    return directories[0]


def open_wechat_database(account: str | None):
    db = _database_module()
    root = db.auto_detect_db_dir()
    if not root:
        raise RuntimeError("未找到微信数据库目录，请确认本机微信已登录")
    directory = select_account(db._find_account_dirs(root), account)
    started = time.perf_counter()
    master = load_master_key(directory)
    from_cache = master is not None
    logger.info(
        "微信消息读取: provider=chatlog 加密缓存 %s elapsed=%.3fs",
        "hit（HMAC 校验通过）" if from_cache else "miss",
        time.perf_counter() - started,
    )
    if master is None:
        scan_started = time.perf_counter()
        logger.info("微信消息读取: provider=chatlog 开始只读密钥扫描（限时 120 秒）")
        master = recover_master_key(str(directory))
        logger.info(
            "微信消息读取: 密钥扫描完成 elapsed=%.3fs",
            time.perf_counter() - scan_started,
        )

    class ChatlogWeChatDB(db.WeChatDB):
        def _save_keys(self):
            # Explicit master-key injection otherwise writes plaintext keys.json.
            # Legacy plaintext caches are not used; our cache uses current-user DPAPI.
            pass

    derive_started = time.perf_counter()
    reader = ChatlogWeChatDB(
        db_dir=str(directory.parent), account=directory.name, master_key=master.hex()
    )
    missing = [str(path).replace("\\", "/") for path in reader.unkeyed]
    if any(
        path == "contact/contact.db" or re.fullmatch(r"message/message_\d+\.db", path)
        for path in missing
    ):
        raise RuntimeError(
            "微信联系人或消息数据库密钥校验失败；未启动消息监听，请检查账号和微信版本"
        )
    logger.info(
        "微信消息读取: 数据库密钥派生与校验 elapsed=%.3fs",
        time.perf_counter() - derive_started,
    )
    if not from_cache:
        save_master_key(directory, master)
    del master
    logger.info(
        "微信消息读取: provider=chatlog 密钥已验证，沿用增量监听 elapsed=%.3fs",
        time.perf_counter() - started,
    )
    return reader
