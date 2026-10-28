"""The SPARK 0.2.0 schema, frozen.

Recorded statement by statement from ``initialise_schema`` at the v0.2.0 tag
(``{auto}`` stands for the backend's auto-increment primary key). The baseline
migration and the one-time legacy SQLite upgrade are built from these lists; do
not edit them. Later schema changes are new migrations.
"""

from __future__ import annotations

__all__ = ["ALTERS", "INDICES", "TABLES"]

# CREATE TABLE IF NOT EXISTS, in 0.2.0 order.
TABLES: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS conversations (\n            id {auto},\n            name TEXT NOT NULL,\n            model_id TEXT,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            total_tokens INTEGER DEFAULT 0,\n            is_active INTEGER DEFAULT 1,\n            instructions TEXT,\n            tokens_sent INTEGER DEFAULT 0,\n            tokens_received INTEGER DEFAULT 0,\n            max_tokens INTEGER,\n            compaction_threshold REAL,\n            compaction_model TEXT,\n            compaction_summary_ratio REAL,\n            web_search_enabled INTEGER DEFAULT 0,\n            memory_enabled INTEGER DEFAULT 1,\n            rag_enabled INTEGER DEFAULT 1,\n            rag_top_k INTEGER DEFAULT 5,\n            rag_threshold REAL DEFAULT 0.4,\n            rag_tool_enabled INTEGER DEFAULT 0,\n            max_history_messages INTEGER,\n            include_tool_results INTEGER DEFAULT 1,\n            prompt_caching INTEGER DEFAULT 1,\n            is_favourite INTEGER DEFAULT 0,\n            user_guid TEXT NOT NULL DEFAULT ''\n        )",
    "CREATE TABLE IF NOT EXISTS messages (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            role TEXT NOT NULL,\n            content TEXT NOT NULL,\n            token_count INTEGER DEFAULT 0,\n            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            is_rolled_up INTEGER DEFAULT 0,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (conversation_id) REFERENCES conversations(id)\n        )",
    "CREATE TABLE IF NOT EXISTS rollup_history (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            original_message_count INTEGER,\n            summarised_content TEXT,\n            original_token_count INTEGER,\n            summarised_token_count INTEGER,\n            rollup_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (conversation_id) REFERENCES conversations(id)\n        )",
    "CREATE TABLE IF NOT EXISTS conversation_files (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            filename TEXT NOT NULL,\n            file_type TEXT,\n            file_size INTEGER,\n            content_text TEXT,\n            content_base64 TEXT,\n            mime_type TEXT,\n            token_count INTEGER DEFAULT 0,\n            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            tags TEXT,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (conversation_id) REFERENCES conversations(id)\n        )",
    "CREATE TABLE IF NOT EXISTS conversation_links (\n            id {auto},\n            source_conversation_id INTEGER NOT NULL,\n            target_conversation_id INTEGER NOT NULL,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (source_conversation_id) REFERENCES conversations(id),\n            FOREIGN KEY (target_conversation_id) REFERENCES conversations(id),\n            UNIQUE(source_conversation_id, target_conversation_id)\n        )",
    "CREATE TABLE IF NOT EXISTS mcp_transactions (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            message_id INTEGER,\n            user_prompt TEXT,\n            tool_name TEXT NOT NULL,\n            tool_server TEXT,\n            tool_input TEXT,\n            tool_response TEXT,\n            is_error INTEGER DEFAULT 0,\n            execution_time_ms INTEGER,\n            transaction_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (conversation_id) REFERENCES conversations(id)\n        )",
    "CREATE TABLE IF NOT EXISTS conversation_model_usage (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            model_id TEXT NOT NULL,\n            input_tokens INTEGER DEFAULT 0,\n            output_tokens INTEGER DEFAULT 0,\n            first_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (conversation_id) REFERENCES conversations(id),\n            UNIQUE(conversation_id, model_id)\n        )",
    "CREATE TABLE IF NOT EXISTS conversation_mcp_servers (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            server_name TEXT NOT NULL,\n            enabled INTEGER DEFAULT 1,\n            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (conversation_id) REFERENCES conversations(id),\n            UNIQUE(conversation_id, server_name)\n        )",
    "CREATE TABLE IF NOT EXISTS conversation_embedded_tools (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            tool_name TEXT NOT NULL,\n            enabled INTEGER DEFAULT 1,\n            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (conversation_id) REFERENCES conversations(id),\n            UNIQUE(conversation_id, tool_name, user_guid)\n        )",
    "CREATE TABLE IF NOT EXISTS usage_tracking (\n            id {auto},\n            conversation_id INTEGER,\n            model_id TEXT,\n            region TEXT,\n            input_tokens INTEGER DEFAULT 0,\n            output_tokens INTEGER DEFAULT 0,\n            cost REAL DEFAULT 0.0,\n            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT ''\n        )",
    "CREATE TABLE IF NOT EXISTS prompt_inspection_violations (\n            id {auto},\n            user_guid TEXT NOT NULL DEFAULT '',\n            conversation_id INTEGER,\n            violation_type TEXT,\n            severity TEXT,\n            prompt_snippet TEXT,\n            detection_method TEXT,\n            action_taken TEXT,\n            confidence_score REAL,\n            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n        )",
    "CREATE TABLE IF NOT EXISTS conversation_tool_permissions (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            tool_name TEXT NOT NULL,\n            permission_state TEXT NOT NULL,\n            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (conversation_id) REFERENCES conversations(id),\n            UNIQUE(conversation_id, tool_name)\n        )",
    "CREATE TABLE IF NOT EXISTS global_tool_permissions (\n            id {auto},\n            user_guid TEXT NOT NULL,\n            tool_name TEXT NOT NULL,\n            permission_state TEXT NOT NULL,\n            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            UNIQUE(user_guid, tool_name)\n        )",
    "CREATE TABLE IF NOT EXISTS autonomous_actions (\n            id {auto},\n            name TEXT UNIQUE NOT NULL,\n            description TEXT,\n            action_prompt TEXT NOT NULL,\n            model_id TEXT NOT NULL,\n            schedule_type TEXT NOT NULL DEFAULT 'one_off',\n            schedule_config TEXT,\n            context_mode TEXT NOT NULL DEFAULT 'fresh',\n            max_failures INTEGER DEFAULT 3,\n            failure_count INTEGER DEFAULT 0,\n            is_enabled INTEGER DEFAULT 1,\n            max_tokens INTEGER DEFAULT 8192,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            last_run_at TIMESTAMP,\n            next_run_at TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            version INTEGER DEFAULT 1,\n            locked_by TEXT,\n            locked_at TIMESTAMP,\n            updated_at TIMESTAMP\n        )",
    "CREATE TABLE IF NOT EXISTS action_runs (\n            id {auto},\n            action_id INTEGER NOT NULL,\n            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            completed_at TIMESTAMP,\n            status TEXT NOT NULL DEFAULT 'running',\n            result_text TEXT,\n            result_html TEXT,\n            error_message TEXT,\n            input_tokens INTEGER DEFAULT 0,\n            output_tokens INTEGER DEFAULT 0,\n            context_snapshot TEXT,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (action_id) REFERENCES autonomous_actions(id)\n        )",
    "CREATE TABLE IF NOT EXISTS action_tool_permissions (\n            id {auto},\n            action_id INTEGER NOT NULL,\n            tool_name TEXT NOT NULL,\n            server_name TEXT,\n            permission_state TEXT NOT NULL,\n            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (action_id) REFERENCES autonomous_actions(id),\n            UNIQUE(action_id, tool_name)\n        )",
    "CREATE TABLE IF NOT EXISTS agent_runs (\n            id {auto},\n            agent_id TEXT UNIQUE NOT NULL,\n            parent_conversation_id INTEGER NOT NULL,\n            agent_name TEXT NOT NULL,\n            task_description TEXT,\n            mode TEXT NOT NULL DEFAULT 'orchestrator',\n            model_id TEXT,\n            status TEXT NOT NULL DEFAULT 'running',\n            result_text TEXT,\n            tool_calls_json TEXT,\n            input_tokens INTEGER DEFAULT 0,\n            output_tokens INTEGER DEFAULT 0,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            completed_at TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (parent_conversation_id) REFERENCES conversations(id)\n        )",
    "CREATE TABLE IF NOT EXISTS context_index_elements (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            element_type TEXT NOT NULL,\n            content_hash TEXT NOT NULL,\n            content_text TEXT NOT NULL,\n            embedding BLOB,\n            metadata_json TEXT,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            user_guid TEXT NOT NULL DEFAULT '',\n            FOREIGN KEY (conversation_id) REFERENCES conversations(id)\n        )",
    "CREATE TABLE IF NOT EXISTS user_memories (\n            id {auto},\n            user_guid TEXT NOT NULL,\n            content TEXT NOT NULL,\n            category TEXT NOT NULL DEFAULT 'facts',\n            content_hash TEXT NOT NULL,\n            embedding BLOB,\n            importance REAL DEFAULT 0.5,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            last_accessed TIMESTAMP,\n            source_conversation_id INTEGER,\n            metadata_json TEXT,\n            UNIQUE(user_guid, content_hash)\n        )",
    "CREATE TABLE IF NOT EXISTS daemon_registry (\n            id {auto},\n            daemon_id TEXT UNIQUE NOT NULL,\n            hostname TEXT,\n            pid INTEGER,\n            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            status TEXT DEFAULT 'running',\n            user_guid TEXT\n        )",
    "CREATE TABLE IF NOT EXISTS debate_config (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            topic TEXT NOT NULL,\n            rounds_mode TEXT NOT NULL DEFAULT 'fixed',\n            max_rounds INTEGER,\n            state TEXT NOT NULL DEFAULT 'setup',\n            current_round INTEGER DEFAULT 0,\n            opening_speaker TEXT,\n            user_guid TEXT NOT NULL,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n        )",
    "CREATE TABLE IF NOT EXISTS debate_agents (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            role TEXT NOT NULL,\n            model_id TEXT NOT NULL,\n            brief TEXT,\n            tokens_sent INTEGER DEFAULT 0,\n            tokens_received INTEGER DEFAULT 0\n        )",
    "CREATE TABLE IF NOT EXISTS debate_turns (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            round INTEGER NOT NULL,\n            role TEXT NOT NULL,\n            turn_type TEXT NOT NULL,\n            content TEXT,\n            summary TEXT,\n            status TEXT DEFAULT 'complete',\n            token_count INTEGER DEFAULT 0,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n        )",
    "CREATE TABLE IF NOT EXISTS debate_exhibits (\n            id {auto},\n            turn_id INTEGER NOT NULL,\n            label TEXT NOT NULL,\n            title TEXT,\n            content TEXT,\n            source TEXT,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n        )",
    "CREATE TABLE IF NOT EXISTS skills (\n            id {auto},\n            name TEXT NOT NULL,\n            enabled INTEGER DEFAULT 1,\n            user_guid TEXT NOT NULL,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n        )",
    "CREATE TABLE IF NOT EXISTS conversation_skills (\n            id {auto},\n            conversation_id INTEGER NOT NULL,\n            skill_name TEXT NOT NULL,\n            enabled INTEGER DEFAULT 1\n        )",
    "CREATE TABLE IF NOT EXISTS kg_nodes (\n            id {auto},\n            scope TEXT NOT NULL,\n            name TEXT NOT NULL,\n            name_key TEXT NOT NULL,\n            entity_type TEXT DEFAULT 'other',\n            description TEXT,\n            embedding BLOB,\n            weight INTEGER DEFAULT 1,\n            user_guid TEXT NOT NULL,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n        )",
    "CREATE TABLE IF NOT EXISTS kg_edges (\n            id {auto},\n            scope TEXT NOT NULL,\n            source_node_id INTEGER NOT NULL,\n            target_node_id INTEGER NOT NULL,\n            relation TEXT NOT NULL,\n            description TEXT,\n            weight INTEGER DEFAULT 1,\n            user_guid TEXT NOT NULL,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n        )",
    "CREATE TABLE IF NOT EXISTS voice_usage (\n            id {auto},\n            conversation_id INTEGER,\n            user_guid TEXT NOT NULL,\n            characters INTEGER NOT NULL,\n            model_id TEXT,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n        )",
    "CREATE TABLE IF NOT EXISTS kg_builds (\n            id {auto},\n            scope TEXT NOT NULL,\n            source_key TEXT NOT NULL,\n            watermark INTEGER DEFAULT 0,\n            built_at TIMESTAMP,\n            status TEXT DEFAULT 'complete',\n            error TEXT,\n            user_guid TEXT NOT NULL\n        )",
)

# CREATE INDEX IF NOT EXISTS, in 0.2.0 order.
INDICES: tuple[str, ...] = (
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
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_conv ON agent_runs(parent_conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_context_idx_conv ON context_index_elements(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_context_idx_hash ON context_index_elements(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_memories_user ON user_memories(user_guid)",
    "CREATE INDEX IF NOT EXISTS idx_memories_category ON user_memories(category)",
    "CREATE INDEX IF NOT EXISTS idx_memories_hash ON user_memories(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_memories_importance ON user_memories(importance)",
    "CREATE INDEX IF NOT EXISTS idx_debate_config_conv ON debate_config(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_debate_agents_conv ON debate_agents(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_debate_turns_conv ON debate_turns(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_debate_exhibits_turn ON debate_exhibits(turn_id)",
    "CREATE INDEX IF NOT EXISTS idx_skills_user_name ON skills(user_guid, name)",
    "CREATE INDEX IF NOT EXISTS idx_conv_skills_conv ON conversation_skills(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_kg_nodes_scope_key ON kg_nodes(scope, name_key)",
    "CREATE INDEX IF NOT EXISTS idx_kg_nodes_user ON kg_nodes(user_guid)",
    "CREATE INDEX IF NOT EXISTS idx_kg_edges_scope ON kg_edges(scope)",
    "CREATE INDEX IF NOT EXISTS idx_kg_builds_scope_source ON kg_builds(scope, source_key)",
)

# 0.2.0's _migrate_schema: each ALTER was attempted and a duplicate column ignored.
ALTERS: tuple[str, ...] = (
    "ALTER TABLE conversations ADD COLUMN rag_enabled INTEGER DEFAULT 1",
    "ALTER TABLE conversations ADD COLUMN rag_top_k INTEGER DEFAULT 5",
    "ALTER TABLE conversations ADD COLUMN rag_threshold REAL DEFAULT 0.4",
    "ALTER TABLE conversations ADD COLUMN rag_tool_enabled INTEGER DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN max_history_messages INTEGER",
    "ALTER TABLE conversations ADD COLUMN include_tool_results INTEGER DEFAULT 1",
    "ALTER TABLE conversations ADD COLUMN is_favourite INTEGER DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN prompt_caching INTEGER DEFAULT 1",
    "ALTER TABLE conversations ADD COLUMN agents_enabled INTEGER DEFAULT 1",
    "ALTER TABLE conversations ADD COLUMN agent_mode TEXT DEFAULT NULL",
    "ALTER TABLE conversations ADD COLUMN agent_model_selection TEXT DEFAULT NULL",
    "ALTER TABLE conversations ADD COLUMN conversation_type TEXT DEFAULT 'standard'",
    "ALTER TABLE conversations ADD COLUMN kg_local_enabled INTEGER DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN kg_use_global INTEGER DEFAULT 1",
    "ALTER TABLE conversations ADD COLUMN kg_auto_context INTEGER DEFAULT 1",
    "ALTER TABLE debate_agents ADD COLUMN allowed_tools TEXT",
    "ALTER TABLE debate_agents ADD COLUMN allowed_skills TEXT",
    "ALTER TABLE debate_agents ADD COLUMN display_name TEXT",
    "ALTER TABLE debate_agents ADD COLUMN is_human INTEGER DEFAULT 0",
    "ALTER TABLE debate_agents ADD COLUMN voice_id TEXT",
)
