from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_bridge.models import (
    Attachment,
    ChannelTarget,
    ContentType,
    ConversationPreferences,
    ConversationType,
    EventType,
    Job,
    JobStatus,
    NativeSession,
    OutboundDelivery,
    OutboundDeliveryStatus,
    ReplyReference,
    UnifiedEvent,
    UnifiedMessage,
    UnifiedSession,
    new_id,
    utc_now,
)

SCHEMA_VERSION = 6


class SQLiteRepository:
    """Small synchronous repository guarded for use from callback threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS unified_sessions (
                    id TEXT PRIMARY KEY,
                    current_provider TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel TEXT NOT NULL,
                    channel_account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    conversation_type TEXT NOT NULL,
                    unified_session_id TEXT NOT NULL REFERENCES unified_sessions(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (channel, channel_account_id, conversation_id)
                );

                CREATE TABLE IF NOT EXISTS native_sessions (
                    id TEXT PRIMARY KEY,
                    unified_session_id TEXT NOT NULL REFERENCES unified_sessions(id),
                    provider TEXT NOT NULL,
                    native_session_id TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    model TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    context_initialized INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_native_active
                    ON native_sessions(unified_session_id, provider, is_active);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    unified_session_id TEXT NOT NULL REFERENCES unified_sessions(id),
                    channel TEXT,
                    channel_message_id TEXT,
                    role TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    provider TEXT,
                    content_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    summarized INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_time
                    ON messages(unified_session_id, created_at);

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    unified_session_id TEXT NOT NULL REFERENCES unified_sessions(id),
                    provider TEXT NOT NULL,
                    inbound_message_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_events (
                    id TEXT PRIMARY KEY,
                    unified_session_id TEXT NOT NULL REFERENCES unified_sessions(id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inbound_dedup (
                    channel TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (channel, message_id)
                );

                CREATE TABLE IF NOT EXISTS channel_state (
                    channel TEXT NOT NULL,
                    channel_account_id TEXT NOT NULL,
                    state_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel, channel_account_id, state_key)
                );

                CREATE TABLE IF NOT EXISTS conversation_preferences (
                    channel TEXT NOT NULL,
                    channel_account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    conversation_type TEXT NOT NULL,
                    reply_enabled INTEGER NOT NULL DEFAULT 0 CHECK(reply_enabled IN (0, 1)),
                    send_images_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(send_images_enabled IN (0, 1)),
                    load_history INTEGER NOT NULL DEFAULT 0 CHECK(load_history IN (0, 1)),
                    history_limit INTEGER NOT NULL DEFAULT 50
                        CHECK(history_limit BETWEEN 1 AND 500),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel, channel_account_id, conversation_id)
                );

                CREATE TABLE IF NOT EXISTS outbound_deliveries (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    channel TEXT NOT NULL,
                    channel_account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    conversation_type TEXT NOT NULL,
                    text_payload TEXT NOT NULL,
                    payload_kind TEXT NOT NULL DEFAULT 'text',
                    attachment_json TEXT NOT NULL DEFAULT '[]',
                    reply_reference_json TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbound_ready
                    ON outbound_deliveries(status, next_attempt_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_outbound_conversation
                    ON outbound_deliveries(
                        channel, channel_account_id, conversation_id, created_at
                    );
                """
            )
            self._ensure_column(
                "conversation_preferences",
                "send_images_enabled",
                "INTEGER NOT NULL DEFAULT 0 CHECK(send_images_enabled IN (0, 1))",
            )
            self._ensure_column(
                "outbound_deliveries",
                "payload_kind",
                "TEXT NOT NULL DEFAULT 'text'",
            )
            self._ensure_column(
                "outbound_deliveries",
                "attachment_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                "outbound_deliveries",
                "reply_reference_json",
                "TEXT",
            )
            self._migrate_native_sessions_allow_shared()
            self._connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _migrate_native_sessions_allow_shared(self) -> None:
        row = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'native_sessions'"
        ).fetchone()
        schema = str(row["sql"] or "") if row else ""
        normalized_schema = "".join(schema.lower().split())
        if "unique(provider,native_session_id)" not in normalized_schema:
            return
        self._connection.execute("DROP INDEX IF EXISTS idx_native_active")
        self._connection.execute("ALTER TABLE native_sessions RENAME TO native_sessions_legacy")
        self._connection.execute(
            """CREATE TABLE native_sessions (
                id TEXT PRIMARY KEY,
                unified_session_id TEXT NOT NULL REFERENCES unified_sessions(id),
                provider TEXT NOT NULL,
                native_session_id TEXT NOT NULL,
                working_directory TEXT NOT NULL,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                is_active INTEGER NOT NULL DEFAULT 1,
                context_initialized INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self._connection.execute(
            """INSERT INTO native_sessions
               (id, unified_session_id, provider, native_session_id, working_directory,
                model, status, is_active, context_initialized, created_at, updated_at)
               SELECT id, unified_session_id, provider, native_session_id, working_directory,
                      model, status, is_active, context_initialized, created_at, updated_at
               FROM native_sessions_legacy"""
        )
        self._connection.execute("DROP TABLE native_sessions_legacy")
        self._connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_native_active
               ON native_sessions(unified_session_id, provider, is_active)"""
        )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def create_session(
        self, current_provider: str, working_directory: str, session_id: str | None = None
    ) -> UnifiedSession:
        session = UnifiedSession(
            id=session_id or new_id("session"),
            current_provider=current_provider,
            working_directory=working_directory,
        )
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO unified_sessions
                   (id, current_provider, working_directory, summary, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.id,
                    session.current_provider,
                    session.working_directory,
                    session.summary,
                    session.status,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )
        return session

    def get_session(self, session_id: str) -> UnifiedSession | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM unified_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._session_from_row(row) if row else None

    def list_sessions(self) -> list[UnifiedSession]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM unified_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def set_current_provider(self, session_id: str, provider: str) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connection:
            result = self._connection.execute(
                "UPDATE unified_sessions SET current_provider = ?, updated_at = ? WHERE id = ?",
                (provider, now, session_id),
            )
            if result.rowcount != 1:
                raise KeyError(f"Unknown session: {session_id}")

    def update_summary(self, session_id: str, summary: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE unified_sessions SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, utc_now().isoformat(), session_id),
            )

    def bind_channel(self, message: UnifiedMessage, session_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO channel_bindings
                   (channel, channel_account_id, conversation_id, conversation_type,
                    unified_session_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(channel, channel_account_id, conversation_id)
                   DO UPDATE SET unified_session_id=excluded.unified_session_id,
                                 conversation_type=excluded.conversation_type""",
                (
                    message.channel,
                    message.channel_account_id,
                    message.conversation_id,
                    message.conversation_type.value,
                    session_id,
                    utc_now().isoformat(),
                ),
            )

    def find_session_for_message(self, message: UnifiedMessage) -> UnifiedSession | None:
        return self.find_session_for_binding(*message.binding_key)

    def find_session_for_binding(
        self, channel: str, channel_account_id: str, conversation_id: str
    ) -> UnifiedSession | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT s.* FROM unified_sessions s
                   JOIN channel_bindings b ON b.unified_session_id = s.id
                   WHERE b.channel = ? AND b.channel_account_id = ? AND b.conversation_id = ?""",
                (channel, channel_account_id, conversation_id),
            ).fetchone()
        return self._session_from_row(row) if row else None

    def add_native_session(self, native: NativeSession) -> None:
        with self._lock, self._connection:
            if native.is_active:
                self._connection.execute(
                    """UPDATE native_sessions SET is_active = 0, updated_at = ?
                       WHERE unified_session_id = ? AND provider = ?""",
                    (utc_now().isoformat(), native.unified_session_id, native.provider),
                )
            self._connection.execute(
                """INSERT INTO native_sessions
                   (id, unified_session_id, provider, native_session_id, working_directory,
                    model, status, is_active, context_initialized, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    native.id,
                    native.unified_session_id,
                    native.provider,
                    native.native_session_id,
                    native.working_directory,
                    native.model,
                    native.status,
                    int(native.is_active),
                    int(native.context_initialized),
                    native.created_at.isoformat(),
                    native.updated_at.isoformat(),
                ),
            )

    def mark_native_initialized(self, native_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE native_sessions SET context_initialized = 1, updated_at = ? WHERE id = ?",
                (utc_now().isoformat(), native_id),
            )

    def get_active_native_session(
        self, unified_session_id: str, provider: str
    ) -> NativeSession | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM native_sessions
                   WHERE unified_session_id = ? AND provider = ? AND is_active = 1
                   ORDER BY updated_at DESC LIMIT 1""",
                (unified_session_id, provider),
            ).fetchone()
        return self._native_from_row(row) if row else None

    def find_native_session(
        self, provider: str, native_session_id: str
    ) -> NativeSession | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM native_sessions
                   WHERE provider = ? AND native_session_id = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (provider, native_session_id),
            ).fetchone()
        return self._native_from_row(row) if row else None

    def activate_native_session(self, native_id: str) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT unified_session_id, provider FROM native_sessions WHERE id = ?",
                (native_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown native session: {native_id}")
            self._connection.execute(
                """UPDATE native_sessions SET is_active = 0, updated_at = ?
                   WHERE unified_session_id = ? AND provider = ?""",
                (utc_now().isoformat(), row["unified_session_id"], row["provider"]),
            )
            self._connection.execute(
                """UPDATE native_sessions SET is_active = 1, updated_at = ?
                   WHERE id = ?""",
                (utc_now().isoformat(), native_id),
            )

    def list_native_sessions(self, unified_session_id: str, provider: str) -> list[NativeSession]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM native_sessions WHERE unified_session_id = ? AND provider = ?
                   ORDER BY is_active DESC, updated_at DESC, rowid DESC""",
                (unified_session_id, provider),
            ).fetchall()
        return [self._native_from_row(row) for row in rows]

    def add_inbound_message(self, session_id: str, message: UnifiedMessage) -> str:
        return self._add_message(
            session_id=session_id,
            role="user",
            event_type=EventType.USER_MESSAGE,
            content=message.content,
            provider=None,
            channel=message.channel,
            channel_message_id=message.message_id,
            metadata={
                "sender_id": message.sender_id,
                "sender_name": message.sender_name,
                "conversation_id": message.conversation_id,
                "bridge_attachments": [
                    {
                        "kind": item.kind,
                        "name": item.name,
                        "path": item.path,
                        "url": item.url,
                        "mime_type": item.mime_type,
                        "metadata": item.metadata,
                    }
                    for item in message.attachments
                ],
                **message.metadata,
            },
            created_at=message.created_at,
        )

    def add_event(self, session_id: str, event: UnifiedEvent) -> str:
        role = "assistant" if event.type == EventType.ASSISTANT_MESSAGE else "system"
        return self._add_message(
            session_id=session_id,
            role=role,
            event_type=event.type,
            content=event.content,
            provider=event.provider,
            metadata=event.metadata,
            created_at=event.created_at,
        )

    def recent_messages(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT id, role, event_type, provider, content_json, metadata_json, created_at
                   FROM messages WHERE unified_session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            result.append(
                {
                    "id": row["id"],
                    "role": row["role"],
                    "event_type": row["event_type"],
                    "provider": row["provider"],
                    "content": json.loads(row["content_json"]),
                    "metadata": json.loads(row["metadata_json"]),
                    "created_at": row["created_at"],
                }
            )
        return result

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return all normalized messages for one unified session chronologically."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT channel, channel_message_id, role, event_type, provider,
                          content_json, metadata_json, created_at
                   FROM messages WHERE unified_session_id = ?
                   ORDER BY created_at, rowid""",
                (session_id,),
            ).fetchall()
        return [
            {
                "channel": row["channel"],
                "channel_message_id": row["channel_message_id"],
                "role": row["role"],
                "event_type": row["event_type"],
                "provider": row["provider"],
                "content": json.loads(row["content_json"]),
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def clear_session_messages(self, session_id: str) -> int:
        """Delete local message history while preserving the session and bindings."""
        now = utc_now().isoformat()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT 1 FROM unified_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(f"Unknown session: {session_id}")
            result = self._connection.execute(
                "DELETE FROM messages WHERE unified_session_id = ?", (session_id,)
            )
            self._connection.execute(
                "UPDATE unified_sessions SET summary = '', updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return int(result.rowcount)

    def rollup_summary(self, session_id: str, retain: int = 40, max_chars: int = 12000) -> str:
        """Deterministically compact older normalized events into the session summary."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT id, role, provider, content_json FROM messages
                   WHERE unified_session_id = ? AND summarized = 0
                   ORDER BY created_at, rowid""",
                (session_id,),
            ).fetchall()
            session = self._connection.execute(
                "SELECT summary FROM unified_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")
        if len(rows) <= retain:
            return session["summary"]

        compacted = rows[:-retain]
        lines: list[str] = []
        for row in compacted:
            content = json.loads(row["content_json"])
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            text = " ".join(text.split())
            if len(text) > 300:
                text = text[:297] + "..."
            provider = f"/{row['provider']}" if row["provider"] else ""
            lines.append(f"- {row['role']}{provider}: {text}")
        addition = "\n".join(lines)
        summary = "\n".join(part for part in (session["summary"], addition) if part)
        if len(summary) > max_chars:
            summary = "[Earlier context omitted]\n" + summary[-max_chars:]

        ids = [row["id"] for row in compacted]
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE messages SET summarized = 1 WHERE id IN ({placeholders})", ids
            )
            self._connection.execute(
                "UPDATE unified_sessions SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, utc_now().isoformat(), session_id),
            )
        return summary

    def create_job(self, job: Job) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO jobs
                   (id, unified_session_id, provider, inbound_message_id, status, error,
                    created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.id,
                    job.unified_session_id,
                    job.provider,
                    job.inbound_message_id,
                    job.status.value,
                    job.error,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )

    def update_job(self, job_id: str, status: JobStatus, error: str | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status.value, error, utc_now().isoformat(), job_id),
            )

    def add_session_event(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> str:
        event_id = new_id("event")
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO session_events
                   (id, unified_session_id, event_type, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    event_id,
                    session_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    utc_now().isoformat(),
                ),
            )
        return event_id

    def interrupt_running_jobs(self) -> int:
        """Discard jobs that cannot survive a bridge process restart.

        Agent work is deliberately held in :class:`SessionTaskQueue`, which is
        an in-memory queue.  Neither queued nor currently running jobs can be
        resumed safely after a restart, but their inbound messages remain in
        ``messages`` and therefore remain part of the conversation history.
        """
        with self._lock, self._connection:
            result = self._connection.execute(
                """UPDATE jobs
                   SET status = ?, error = ?, updated_at = ?
                   WHERE status IN (?, ?)""",
                (
                    JobStatus.INTERRUPTED.value,
                    "service_restarted",
                    utc_now().isoformat(),
                    JobStatus.RUNNING.value,
                    JobStatus.QUEUED.value,
                ),
            )
        return result.rowcount

    def mark_inbound_processed(self, channel: str, message_id: str) -> bool:
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    "INSERT INTO inbound_dedup(channel, message_id, processed_at) VALUES (?, ?, ?)",
                    (channel, message_id, utc_now().isoformat()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def get_conversation_preferences(
        self,
        channel: str,
        channel_account_id: str,
        conversation_id: str,
        conversation_type: ConversationType,
    ) -> ConversationPreferences:
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM conversation_preferences
                   WHERE channel = ? AND channel_account_id = ? AND conversation_id = ?""",
                (channel, channel_account_id, conversation_id),
            ).fetchone()
        if row is None:
            return ConversationPreferences(
                channel,
                channel_account_id,
                conversation_id,
                conversation_type,
            )
        return ConversationPreferences(
            channel=row["channel"],
            channel_account_id=row["channel_account_id"],
            conversation_id=row["conversation_id"],
            conversation_type=ConversationType(row["conversation_type"]),
            reply_enabled=bool(row["reply_enabled"]),
            send_images_enabled=bool(row["send_images_enabled"]),
            load_history=bool(row["load_history"]),
            history_limit=int(row["history_limit"]),
        )

    def set_conversation_preferences(
        self, preferences: ConversationPreferences
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO conversation_preferences
                   (channel, channel_account_id, conversation_id, conversation_type,
                    reply_enabled, send_images_enabled, load_history, history_limit,
                    updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(channel, channel_account_id, conversation_id)
                   DO UPDATE SET
                       conversation_type=excluded.conversation_type,
                       reply_enabled=excluded.reply_enabled,
                       send_images_enabled=excluded.send_images_enabled,
                       load_history=excluded.load_history,
                       history_limit=excluded.history_limit,
                       updated_at=excluded.updated_at""",
                (
                    preferences.channel,
                    preferences.channel_account_id,
                    preferences.conversation_id,
                    preferences.conversation_type.value,
                    int(preferences.reply_enabled),
                    int(preferences.send_images_enabled),
                    int(preferences.load_history),
                    preferences.history_limit,
                    utc_now().isoformat(),
                ),
            )

    def set_channel_state(
        self, channel: str, channel_account_id: str, state_key: str, value: Any
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO channel_state
                   (channel, channel_account_id, state_key, value_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(channel, channel_account_id, state_key)
                   DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (
                    channel,
                    channel_account_id,
                    state_key,
                    json.dumps(value, ensure_ascii=False),
                    utc_now().isoformat(),
                ),
            )

    def get_channel_state(
        self, channel: str, channel_account_id: str, state_key: str
    ) -> Any | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT value_json FROM channel_state
                   WHERE channel = ? AND channel_account_id = ? AND state_key = ?""",
                (channel, channel_account_id, state_key),
            ).fetchone()
        return json.loads(row["value_json"]) if row else None

    def enqueue_outbound_delivery(
        self,
        target: ChannelTarget,
        text: str,
        idempotency_key: str,
        max_queue_age_seconds: float,
        *,
        now: datetime | None = None,
        content_type: ContentType = ContentType.TEXT,
        attachments: tuple[Attachment, ...] = (),
        reply_to: ReplyReference | None = None,
    ) -> OutboundDelivery:
        created_at = now or utc_now()
        delivery_id = new_id("wechat_outbound")
        expires_at = created_at + timedelta(seconds=max_queue_age_seconds)
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO outbound_deliveries
                   (id, idempotency_key, channel, channel_account_id,
                    conversation_id, conversation_type, text_payload, payload_kind,
                    attachment_json, reply_reference_json, status, attempts,
                    created_at, next_attempt_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                   ON CONFLICT(idempotency_key) DO NOTHING""",
                (
                    delivery_id,
                    idempotency_key,
                    "wechat",
                    target.channel_account_id,
                    target.conversation_id,
                    target.conversation_type.value,
                    text,
                    content_type.value,
                    json.dumps(
                        [
                            {
                                "kind": item.kind,
                                "name": item.name,
                                "path": item.path,
                                "url": item.url,
                                "mime_type": item.mime_type,
                                "metadata": item.metadata,
                            }
                            for item in attachments
                        ],
                        ensure_ascii=False,
                    ),
                    self._reply_reference_json(reply_to),
                    OutboundDeliveryStatus.QUEUED.value,
                    created_at.isoformat(),
                    created_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM outbound_deliveries WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        assert row is not None
        return self._outbound_from_row(row)

    def claim_next_outbound_delivery(
        self, *, now: datetime | None = None
    ) -> OutboundDelivery | None:
        claimed_at = now or utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE outbound_deliveries
                   SET status = ?, completed_at = ?
                   WHERE status IN (?, ?, ?)
                     AND expires_at <= ?""",
                (
                    OutboundDeliveryStatus.EXPIRED.value,
                    claimed_at.isoformat(),
                    OutboundDeliveryStatus.QUEUED.value,
                    OutboundDeliveryStatus.WAITING_FOR_IDLE.value,
                    OutboundDeliveryStatus.RETRY_WAIT.value,
                    claimed_at.isoformat(),
                ),
            )
            row = self._connection.execute(
                """SELECT * FROM outbound_deliveries
                   WHERE status IN (?, ?, ?)
                     AND expires_at > ?
                   ORDER BY created_at, rowid
                   LIMIT 1""",
                (
                    OutboundDeliveryStatus.QUEUED.value,
                    OutboundDeliveryStatus.WAITING_FOR_IDLE.value,
                    OutboundDeliveryStatus.RETRY_WAIT.value,
                    claimed_at.isoformat(),
                ),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row["next_attempt_at"]) > claimed_at:
                return None
            self._connection.execute(
                """UPDATE outbound_deliveries SET status = ?
                   WHERE id = ?""",
                (OutboundDeliveryStatus.WAITING_FOR_IDLE.value, row["id"]),
            )
            row = self._connection.execute(
                "SELECT * FROM outbound_deliveries WHERE id = ?", (row["id"],)
            ).fetchone()
        return self._outbound_from_row(row)

    def update_outbound_delivery(
        self,
        delivery_id: str,
        status: OutboundDeliveryStatus,
        *,
        attempts: int | None = None,
        next_attempt_at: datetime | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        completed_at: datetime | None = None,
    ) -> OutboundDelivery:
        assignments = ["status = ?", "error_code = ?", "error_message = ?"]
        values: list[Any] = [status.value, error_code, error_message]
        if attempts is not None:
            assignments.append("attempts = ?")
            values.append(attempts)
        if next_attempt_at is not None:
            assignments.append("next_attempt_at = ?")
            values.append(next_attempt_at.isoformat())
        if completed_at is not None:
            assignments.append("completed_at = ?")
            values.append(completed_at.isoformat())
        values.append(delivery_id)
        with self._lock, self._connection:
            result = self._connection.execute(
                f"UPDATE outbound_deliveries SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if result.rowcount != 1:
                raise KeyError(f"Unknown outbound delivery: {delivery_id}")
            row = self._connection.execute(
                "SELECT * FROM outbound_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return self._outbound_from_row(row)

    def get_outbound_delivery(self, delivery_id: str) -> OutboundDelivery | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM outbound_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return self._outbound_from_row(row) if row else None

    def list_outbound_deliveries(
        self, channel_account_id: str, conversation_id: str
    ) -> list[OutboundDelivery]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM outbound_deliveries
                   WHERE channel = 'wechat' AND channel_account_id = ?
                     AND conversation_id = ?
                   ORDER BY created_at, rowid""",
                (channel_account_id, conversation_id),
            ).fetchall()
        return [self._outbound_from_row(row) for row in rows]

    def retry_outbound_delivery(
        self, delivery_id: str, max_queue_age_seconds: float
    ) -> OutboundDelivery:
        original = self.get_outbound_delivery(delivery_id)
        if original is None:
            raise KeyError(f"Unknown outbound delivery: {delivery_id}")
        if original.status not in {
            OutboundDeliveryStatus.FAILED,
            OutboundDeliveryStatus.EXPIRED,
        }:
            raise ValueError("Only failed or expired deliveries can be retried")
        return self.enqueue_outbound_delivery(
            ChannelTarget(
                original.channel_account_id,
                original.conversation_id,
                original.conversation_type,
            ),
            original.text,
            new_id(f"retry_{original.id}"),
            max_queue_age_seconds,
            content_type=original.content_type,
            attachments=original.attachments,
            reply_to=original.reply_to,
        )

    def resend_outbound_delivery(
        self, delivery_id: str, max_queue_age_seconds: float
    ) -> OutboundDelivery:
        original = self.get_outbound_delivery(delivery_id)
        if original is None:
            raise KeyError(f"Unknown outbound delivery: {delivery_id}")
        if original.status != OutboundDeliveryStatus.SENT:
            raise ValueError("Only sent deliveries can be sent again")
        return self.enqueue_outbound_delivery(
            ChannelTarget(
                original.channel_account_id,
                original.conversation_id,
                original.conversation_type,
            ),
            original.text,
            new_id(f"resend_{original.id}"),
            max_queue_age_seconds,
            content_type=original.content_type,
            attachments=original.attachments,
            reply_to=original.reply_to,
        )

    def cancel_outbound_delivery(self, delivery_id: str) -> OutboundDelivery:
        now = utc_now()
        with self._lock, self._connection:
            result = self._connection.execute(
                """UPDATE outbound_deliveries
                   SET status = ?, completed_at = ?, error_code = NULL,
                       error_message = NULL
                   WHERE id = ? AND status IN (?, ?, ?)""",
                (
                    OutboundDeliveryStatus.CANCELLED.value,
                    now.isoformat(),
                    delivery_id,
                    OutboundDeliveryStatus.QUEUED.value,
                    OutboundDeliveryStatus.WAITING_FOR_IDLE.value,
                    OutboundDeliveryStatus.RETRY_WAIT.value,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Only queued or waiting deliveries can be cancelled")
            row = self._connection.execute(
                "SELECT * FROM outbound_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return self._outbound_from_row(row)

    def recover_outbound_deliveries(self) -> int:
        with self._lock, self._connection:
            result = self._connection.execute(
                """UPDATE outbound_deliveries
                   SET status = ?,
                       error_code = CASE
                           WHEN error_code = 'foreground_inflight'
                           THEN 'recovered_foreground_inflight'
                           ELSE 'recovered_sending'
                       END,
                       error_message = ?
                   WHERE status = ?""",
                (
                    OutboundDeliveryStatus.QUEUED.value,
                    "Verifying a send interrupted by process shutdown",
                    OutboundDeliveryStatus.SENDING.value,
                ),
            )
        return result.rowcount

    def discard_pending_outbound_deliveries(self) -> int:
        """Expire unsent replies at bridge startup.

        Outbound rows are normally durable so transient send failures can be
        retried.  A process restart is the explicit boundary for the
        conversation reply queue, however: queued, waiting, retrying, and
        in-flight rows must not be sent later as stale replies.  The rows stay
        in the database for status/history inspection and manual retry.
        """
        now = utc_now()
        with self._lock, self._connection:
            result = self._connection.execute(
                """UPDATE outbound_deliveries
                   SET status = ?, completed_at = ?, error_code = ?,
                       error_message = ?
                   WHERE status IN (?, ?, ?, ?)""",
                (
                    OutboundDeliveryStatus.EXPIRED.value,
                    now.isoformat(),
                    "service_restarted",
                    "Pending reply discarded because the service restarted",
                    OutboundDeliveryStatus.QUEUED.value,
                    OutboundDeliveryStatus.WAITING_FOR_IDLE.value,
                    OutboundDeliveryStatus.RETRY_WAIT.value,
                    OutboundDeliveryStatus.SENDING.value,
                ),
            )
        return result.rowcount

    def list_recovered_outbound_deliveries(self) -> list[OutboundDelivery]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM outbound_deliveries
                   WHERE status = ? AND error_code IN
                       ('recovered_sending', 'recovered_foreground_inflight')
                   ORDER BY created_at, rowid""",
                (OutboundDeliveryStatus.QUEUED.value,),
            ).fetchall()
        return [self._outbound_from_row(row) for row in rows]

    def _add_message(
        self,
        session_id: str,
        role: str,
        event_type: EventType,
        content: Any,
        provider: str | None,
        metadata: dict[str, Any],
        created_at: datetime,
        channel: str | None = None,
        channel_message_id: str | None = None,
    ) -> str:
        message_id = new_id("message")
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO messages
                   (id, unified_session_id, channel, channel_message_id, role, event_type,
                    provider, content_json, metadata_json, summarized, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    message_id,
                    session_id,
                    channel,
                    channel_message_id,
                    role,
                    event_type.value,
                    provider,
                    json.dumps(content, ensure_ascii=False, default=str),
                    json.dumps(metadata, ensure_ascii=False, default=str),
                    created_at.isoformat(),
                ),
            )
        return message_id

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> UnifiedSession:
        return UnifiedSession(
            id=row["id"],
            current_provider=row["current_provider"],
            working_directory=row["working_directory"],
            summary=row["summary"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _native_from_row(row: sqlite3.Row) -> NativeSession:
        return NativeSession(
            id=row["id"],
            unified_session_id=row["unified_session_id"],
            provider=row["provider"],
            native_session_id=row["native_session_id"],
            working_directory=row["working_directory"],
            model=row["model"],
            status=row["status"],
            is_active=bool(row["is_active"]),
            context_initialized=bool(row["context_initialized"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _reply_reference_json(reply_to: ReplyReference | None) -> str | None:
        if reply_to is None:
            return None
        return json.dumps(
            {
                "message_id": reply_to.message_id,
                "conversation_id": reply_to.conversation_id,
                "conversation_type": reply_to.conversation_type.value,
                "sender_id": reply_to.sender_id,
                "sender_name": reply_to.sender_name,
                "content": reply_to.content,
                "content_type": reply_to.content_type.value,
                "sort_seq": reply_to.sort_seq,
                "created_at": reply_to.created_at.isoformat(),
                "occurrence_from_latest": reply_to.occurrence_from_latest,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _outbound_from_row(row: sqlite3.Row) -> OutboundDelivery:
        reply_raw = json.loads(row["reply_reference_json"] or "null")
        reply_to = None
        if reply_raw:
            reply_to = ReplyReference(
                message_id=reply_raw["message_id"],
                conversation_id=reply_raw["conversation_id"],
                conversation_type=ConversationType(reply_raw["conversation_type"]),
                sender_id=reply_raw["sender_id"],
                sender_name=reply_raw.get("sender_name"),
                content=reply_raw["content"],
                content_type=ContentType(reply_raw.get("content_type", "text")),
                sort_seq=reply_raw.get("sort_seq"),
                created_at=datetime.fromisoformat(reply_raw["created_at"]),
                occurrence_from_latest=reply_raw.get("occurrence_from_latest"),
            )
        return OutboundDelivery(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            channel=row["channel"],
            channel_account_id=row["channel_account_id"],
            conversation_id=row["conversation_id"],
            conversation_type=ConversationType(row["conversation_type"]),
            text=row["text_payload"],
            content_type=ContentType(row["payload_kind"]),
            attachments=tuple(
                Attachment(**item)
                for item in json.loads(row["attachment_json"] or "[]")
            ),
            reply_to=reply_to,
            status=OutboundDeliveryStatus(row["status"]),
            attempts=int(row["attempts"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            next_attempt_at=datetime.fromisoformat(row["next_attempt_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
        )
