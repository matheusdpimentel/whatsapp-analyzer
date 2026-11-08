"""Schema migrations, applied in order and tracked with `PRAGMA user_version`.

Never edit a released migration: add a new one. Each migration runs in one transaction together
with the `user_version` update, so a database is never left half-migrated.

Nothing is deleted or cascaded: a conversation that is imported again with different content
gets a new immutable version, and metrics and scores keep pointing at the version they were
computed from.
"""

MIGRATIONS: tuple[str, ...] = (
    # 1: initial schema (milestone 3)
    """
    CREATE TABLE imports (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        tool_version TEXT NOT NULL,
        options_json TEXT NOT NULL,
        report_json TEXT NOT NULL,
        conversations_new INTEGER NOT NULL,
        conversations_changed INTEGER NOT NULL,
        conversations_unchanged INTEGER NOT NULL
    );

    CREATE TABLE sources (
        id INTEGER PRIMARY KEY,
        import_id INTEGER NOT NULL REFERENCES imports(id),
        file_index INTEGER NOT NULL,
        file_name TEXT NOT NULL,
        format TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        records INTEGER NOT NULL,
        UNIQUE (import_id, file_index)
    );

    CREATE TABLE conversations (
        id INTEGER PRIMARY KEY,
        source_format TEXT NOT NULL,
        conversation_key TEXT NOT NULL,
        current_version_id INTEGER REFERENCES conversation_versions(id),
        created_import_id INTEGER NOT NULL REFERENCES imports(id),
        UNIQUE (source_format, conversation_key)
    );

    CREATE TABLE conversation_versions (
        id INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        content_sha256 TEXT NOT NULL,
        message_count INTEGER NOT NULL,
        first_at TEXT,
        last_at TEXT,
        inbound_senders INTEGER NOT NULL,
        is_group INTEGER NOT NULL CHECK (is_group IN (0, 1)),
        import_id INTEGER NOT NULL REFERENCES imports(id),
        source_id INTEGER NOT NULL REFERENCES sources(id),
        UNIQUE (conversation_id, content_sha256)
    );

    CREATE TABLE messages (
        version_id INTEGER NOT NULL REFERENCES conversation_versions(id),
        seq INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        sender TEXT NOT NULL,
        direction TEXT CHECK (direction IN ('inbound', 'outbound')),
        text TEXT NOT NULL,
        message_type TEXT NOT NULL
            CHECK (message_type IN ('text', 'media', 'deleted', 'system')),
        media_kind TEXT,
        PRIMARY KEY (version_id, seq)
    ) WITHOUT ROWID;

    CREATE INDEX messages_by_time ON messages (version_id, timestamp, seq);

    CREATE TABLE metric_configs (
        hash TEXT PRIMARY KEY,
        metrics_version INTEGER NOT NULL,
        config_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE conversation_metrics (
        version_id INTEGER NOT NULL REFERENCES conversation_versions(id),
        metrics_hash TEXT NOT NULL REFERENCES metric_configs(hash),
        is_group INTEGER NOT NULL,
        messages_considered INTEGER NOT NULL,
        inbound_messages INTEGER NOT NULL,
        outbound_messages INTEGER NOT NULL,
        inbound_content_messages INTEGER NOT NULL,
        inbound_deleted_messages INTEGER NOT NULL,
        excluded_system INTEGER NOT NULL,
        excluded_after_reference INTEGER NOT NULL,
        excluded_ignored_text INTEGER NOT NULL,
        excluded_deleted INTEGER NOT NULL,
        collapsed_duplicates INTEGER NOT NULL,
        active_weeks INTEGER NOT NULL,
        outbound_turns INTEGER,
        answered_outbound_turns INTEGER,
        pending_outbound_turns INTEGER,
        sessions INTEGER NOT NULL,
        contact_started_sessions INTEGER,
        last_inbound_at TEXT,
        days_since_last_inbound INTEGER,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (version_id, metrics_hash)
    ) WITHOUT ROWID;

    CREATE TABLE scoring_runs (
        id INTEGER PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN ('running', 'incomplete', 'complete')),
        scoring_hash TEXT NOT NULL,
        scoring_version INTEGER NOT NULL,
        scoring_json TEXT NOT NULL,
        metrics_hash TEXT NOT NULL REFERENCES metric_configs(hash),
        reference_date TEXT NOT NULL,
        reference_date_source TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        conversations_total INTEGER NOT NULL,
        conversations_scored INTEGER NOT NULL DEFAULT 0,
        tool_version TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        last_used_seq INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX scoring_runs_by_key
        ON scoring_runs (scoring_hash, metrics_hash, input_fingerprint);

    CREATE TABLE conversation_scores (
        run_id INTEGER NOT NULL REFERENCES scoring_runs(id),
        version_id INTEGER NOT NULL REFERENCES conversation_versions(id),
        frequency REAL,
        reciprocity REAL,
        initiative REAL,
        recency REAL,
        coverage REAL,
        score REAL,
        level TEXT NOT NULL,
        reason TEXT NOT NULL,
        PRIMARY KEY (run_id, version_id)
    ) WITHOUT ROWID;
    """,
)

LATEST_VERSION = len(MIGRATIONS)
