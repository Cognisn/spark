"""Database schema definition and initialisation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


def initialise_schema(db: DatabaseConnection) -> None:
    """Create all tables and indices if they don't exist."""
    auto = db.backend.autoincrement()

    _create_tables(db, auto)
    _create_indices(db)
    _migrate_schema(db)
    db.commit()
    logger.info("Database schema initialised")


def _create_tables(db: DatabaseConnection, auto: str) -> None:
    tables = [
        f"""CREATE TABLE IF NOT EXISTS conversations (
            id {auto},
            name TEXT NOT NULL,
            model_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_tokens INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            instructions TEXT,
            tokens_sent INTEGER DEFAULT 0,
            tokens_received INTEGER DEFAULT 0,
            max_tokens INTEGER,
            compaction_threshold REAL,
            compaction_model TEXT,
            compaction_summary_ratio REAL,
            web_search_enabled INTEGER DEFAULT 0,
            memory_enabled INTEGER DEFAULT 1,
            rag_enabled INTEGER DEFAULT 1,
            rag_top_k INTEGER DEFAULT 5,
            rag_threshold REAL DEFAULT 0.4,
            rag_tool_enabled INTEGER DEFAULT 0,
            max_history_messages INTEGER,
            include_tool_results INTEGER DEFAULT 1,
            prompt_caching INTEGER DEFAULT 1,
            is_favourite INTEGER DEFAULT 0,
            user_guid TEXT NOT NULL DEFAULT ''
        )""",
        f"""CREATE TABLE IF NOT EXISTS messages (
            id {auto},
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            token_count INTEGER DEFAULT 0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_rolled_up INTEGER DEFAULT 0,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS rollup_history (
            id {auto},
            conversation_id INTEGER NOT NULL,
            original_message_count INTEGER,
            summarised_content TEXT,
            original_token_count INTEGER,
            summarised_token_count INTEGER,
            rollup_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS conversation_files (
            id {auto},
            conversation_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            file_type TEXT,
            file_size INTEGER,
            content_text TEXT,
            content_base64 TEXT,
            mime_type TEXT,
            token_count INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tags TEXT,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS conversation_links (
            id {auto},
            source_conversation_id INTEGER NOT NULL,
            target_conversation_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (source_conversation_id) REFERENCES conversations(id),
            FOREIGN KEY (target_conversation_id) REFERENCES conversations(id),
            UNIQUE(source_conversation_id, target_conversation_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS mcp_transactions (
            id {auto},
            conversation_id INTEGER NOT NULL,
            message_id INTEGER,
            user_prompt TEXT,
            tool_name TEXT NOT NULL,
            tool_server TEXT,
            tool_input TEXT,
            tool_response TEXT,
            is_error INTEGER DEFAULT 0,
            execution_time_ms INTEGER,
            transaction_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS conversation_model_usage (
            id {auto},
            conversation_id INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            first_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id),
            UNIQUE(conversation_id, model_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS conversation_mcp_servers (
            id {auto},
            conversation_id INTEGER NOT NULL,
            server_name TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id),
            UNIQUE(conversation_id, server_name)
        )""",
        f"""CREATE TABLE IF NOT EXISTS conversation_embedded_tools (
            id {auto},
            conversation_id INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id),
            UNIQUE(conversation_id, tool_name, user_guid)
        )""",
        f"""CREATE TABLE IF NOT EXISTS usage_tracking (
            id {auto},
            conversation_id INTEGER,
            model_id TEXT,
            region TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0.0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT ''
        )""",
        f"""CREATE TABLE IF NOT EXISTS prompt_inspection_violations (
            id {auto},
            user_guid TEXT NOT NULL DEFAULT '',
            conversation_id INTEGER,
            violation_type TEXT,
            severity TEXT,
            prompt_snippet TEXT,
            detection_method TEXT,
            action_taken TEXT,
            confidence_score REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS conversation_tool_permissions (
            id {auto},
            conversation_id INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            permission_state TEXT NOT NULL,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id),
            UNIQUE(conversation_id, tool_name)
        )""",
        f"""CREATE TABLE IF NOT EXISTS global_tool_permissions (
            id {auto},
            user_guid TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            permission_state TEXT NOT NULL,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_guid, tool_name)
        )""",
        f"""CREATE TABLE IF NOT EXISTS autonomous_actions (
            id {auto},
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            action_prompt TEXT NOT NULL,
            model_id TEXT NOT NULL,
            schedule_type TEXT NOT NULL DEFAULT 'one_off',
            schedule_config TEXT,
            context_mode TEXT NOT NULL DEFAULT 'fresh',
            max_failures INTEGER DEFAULT 3,
            failure_count INTEGER DEFAULT 0,
            is_enabled INTEGER DEFAULT 1,
            max_tokens INTEGER DEFAULT 8192,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_run_at TIMESTAMP,
            next_run_at TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            version INTEGER DEFAULT 1,
            locked_by TEXT,
            locked_at TIMESTAMP,
            updated_at TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS action_runs (
            id {auto},
            action_id INTEGER NOT NULL,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'running',
            result_text TEXT,
            result_html TEXT,
            error_message TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            context_snapshot TEXT,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (action_id) REFERENCES autonomous_actions(id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS action_tool_permissions (
            id {auto},
            action_id INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            server_name TEXT,
            permission_state TEXT NOT NULL,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (action_id) REFERENCES autonomous_actions(id),
            UNIQUE(action_id, tool_name)
        )""",
        f"""CREATE TABLE IF NOT EXISTS context_index_elements (
            id {auto},
            conversation_id INTEGER NOT NULL,
            element_type TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            content_text TEXT NOT NULL,
            embedding BLOB,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS user_memories (
            id {auto},
            user_guid TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'facts',
            content_hash TEXT NOT NULL,
            embedding BLOB,
            importance REAL DEFAULT 0.5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_accessed TIMESTAMP,
            source_conversation_id INTEGER,
            metadata_json TEXT,
            UNIQUE(user_guid, content_hash)
        )""",
        f"""CREATE TABLE IF NOT EXISTS daemon_registry (
            id {auto},
            daemon_id TEXT UNIQUE NOT NULL,
            hostname TEXT,
            pid INTEGER,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'running',
            user_guid TEXT
        )""",
    ]

    for sql in tables:
        db.execute(sql)


def _create_indices(db: DatabaseConnection) -> None:
    indices = [
        "CREATE INDEX IF NOT EXISTS idx_conversations_active ON conversations(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_guid)",
        "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_files_conversation ON conversation_files(conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_links_source ON conversation_links(source_conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_links_target ON conversation_links(target_conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_mcp_tx_conversation ON mcp_transactions(conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_mcp_tx_timestamp ON mcp_transactions(transaction_timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_mcp_tx_tool ON mcp_transactions(tool_name)",
        "CREATE INDEX IF NOT EXISTS idx_model_usage_conv ON conversation_model_usage(conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_tracking(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_usage_conversation ON usage_tracking(conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_violations_user ON prompt_inspection_violations(user_guid)",
        "CREATE INDEX IF NOT EXISTS idx_violations_timestamp ON prompt_inspection_violations(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_tool_perms_conv ON conversation_tool_permissions(conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_tool_perms_tool ON conversation_tool_permissions(tool_name)",
        "CREATE INDEX IF NOT EXISTS idx_global_perms_user ON global_tool_permissions(user_guid)",
        "CREATE INDEX IF NOT EXISTS idx_global_perms_tool ON global_tool_permissions(tool_name)",
        "CREATE INDEX IF NOT EXISTS idx_actions_enabled ON autonomous_actions(is_enabled)",
        "CREATE INDEX IF NOT EXISTS idx_actions_next_run ON autonomous_actions(next_run_at)",
        "CREATE INDEX IF NOT EXISTS idx_action_runs_action ON action_runs(action_id)",
        "CREATE INDEX IF NOT EXISTS idx_action_runs_status ON action_runs(status)",
        "CREATE INDEX IF NOT EXISTS idx_context_idx_conv ON context_index_elements(conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_context_idx_hash ON context_index_elements(content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_memories_user ON user_memories(user_guid)",
        "CREATE INDEX IF NOT EXISTS idx_memories_category ON user_memories(category)",
        "CREATE INDEX IF NOT EXISTS idx_memories_hash ON user_memories(content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_memories_importance ON user_memories(importance)",
    ]

    for sql in indices:
        db.execute(sql)


def _migrate_schema(db: DatabaseConnection) -> None:
    """Add columns that may be missing from an older schema version.

    Each ALTER TABLE is wrapped in try/except because SQLite raises an error
    if the column already exists. This is idempotent.
    """
    migrations = [
        "ALTER TABLE conversations ADD COLUMN rag_enabled INTEGER DEFAULT 1",
        "ALTER TABLE conversations ADD COLUMN rag_top_k INTEGER DEFAULT 5",
        "ALTER TABLE conversations ADD COLUMN rag_threshold REAL DEFAULT 0.4",
        "ALTER TABLE conversations ADD COLUMN rag_tool_enabled INTEGER DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN max_history_messages INTEGER",
        "ALTER TABLE conversations ADD COLUMN include_tool_results INTEGER DEFAULT 1",
        "ALTER TABLE conversations ADD COLUMN is_favourite INTEGER DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN prompt_caching INTEGER DEFAULT 1",
    ]

    for sql in migrations:
        try:
            db.execute(sql)
        except Exception:
            pass  # Column already exists
