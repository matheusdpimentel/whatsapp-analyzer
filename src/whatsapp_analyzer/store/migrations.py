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
    # 2: persistent LLM runner (milestone 5). These tables never hold message text, prompts or
    # raw provider responses: only hashes, references, statuses, codes, counts and costs.
    """
    CREATE TABLE llm_runs (
        id INTEGER PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN ('running', 'paused', 'interrupted',
            'budget_reached', 'completed', 'completed_with_failures')),
        stop_reason TEXT,
        run_key TEXT NOT NULL,
        scope_fingerprint TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_id TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        template_hash TEXT NOT NULL,
        context_config_hash TEXT NOT NULL,
        generation_json TEXT NOT NULL,
        limits_json TEXT NOT NULL,
        price_json TEXT,
        jobs_total INTEGER NOT NULL,
        tool_version TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        last_used_seq INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX llm_runs_by_key ON llm_runs (run_key, scope_fingerprint);

    CREATE TABLE llm_jobs (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES llm_runs(id),
        version_id INTEGER NOT NULL REFERENCES conversation_versions(id),
        status TEXT NOT NULL CHECK (status IN ('pending', 'in_flight', 'retry_wait',
            'awaiting_reconciliation', 'succeeded', 'failed', 'reused', 'skipped')),
        skip_reason TEXT,
        request_id TEXT,
        context_hash TEXT,
        prompt_hash TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        round_attempts INTEGER NOT NULL DEFAULT 0,
        execution_status TEXT,
        error_kind TEXT,
        validation_error TEXT,
        label TEXT,
        reasons_json TEXT,
        evidence_json TEXT,
        security_flags_json TEXT,
        reused_from_attempt INTEGER REFERENCES llm_attempts(id),
        UNIQUE (run_id, version_id)
    );

    CREATE INDEX llm_jobs_by_status ON llm_jobs (run_id, status);

    CREATE INDEX llm_jobs_by_request ON llm_jobs (run_id, request_id);

    CREATE TABLE llm_attempts (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES llm_jobs(id),
        number INTEGER NOT NULL,
        provider TEXT NOT NULL,
        request_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN ('dispatched', 'completed', 'abandoned')),
        billing TEXT NOT NULL CHECK (billing IN ('pending', 'not_sent', 'rejected', 'billed',
            'unknown')),
        execution_status TEXT,
        error_kind TEXT,
        http_status INTEGER,
        finish_reason TEXT,
        provider_request_id TEXT,
        input_uncached INTEGER,
        input_cache_read INTEGER,
        input_cache_write INTEGER,
        output_total INTEGER,
        output_reasoning INTEGER,
        usage_estimated INTEGER,
        estimated_input_tokens INTEGER NOT NULL,
        max_output_tokens INTEGER NOT NULL,
        reserved_tokens INTEGER NOT NULL,
        reserved_cost TEXT,
        cost TEXT,
        latency_ms INTEGER,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        UNIQUE (job_id, number)
    );

    CREATE INDEX llm_attempts_by_request ON llm_attempts (provider, request_id, execution_status);
    """,
    # 3: authorizations to send content to a real provider (milestone 6). One row per
    # invocation that was allowed to send: when, how, to which destination, and which version
    # of the privacy notice was shown. No message text, prompts or credentials.
    """
    CREATE TABLE llm_authorizations (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES llm_runs (id),
        authorized_at TEXT NOT NULL,
        method TEXT NOT NULL CHECK (method IN ('flag', 'interactive')),
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        notice_version INTEGER NOT NULL
    );

    CREATE INDEX llm_authorizations_by_run ON llm_authorizations (run_id);
    """,
    # 4: independent reviews (milestone 7). A review run is an ordinary run (migration 2);
    # this table only records which primary run it reviews, how its scope was selected and
    # with which consolidation policy. One row per selection (mode, criteria, explicit keys):
    # two selections that pick the same versions share a review run but keep their own row.
    # `used_seq` grows each time a selection is reviewed (planned or resumed), so "the latest
    # review" is the one used last. Results are never copied or overwritten.
    """
    CREATE TABLE llm_reviews (
        id INTEGER PRIMARY KEY,
        primary_run_id INTEGER NOT NULL REFERENCES llm_runs (id),
        review_run_id INTEGER NOT NULL REFERENCES llm_runs (id),
        mode TEXT NOT NULL CHECK (mode IN ('selective', 'dual')),
        selection_key TEXT NOT NULL,
        criteria_json TEXT NOT NULL,
        policy TEXT NOT NULL,
        selected INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        used_seq INTEGER NOT NULL,
        UNIQUE (primary_run_id, review_run_id, mode, selection_key),
        UNIQUE (used_seq),
        CHECK (primary_run_id != review_run_id)
    );

    CREATE INDEX llm_reviews_by_primary ON llm_reviews (primary_run_id, mode, selection_key);
    """,
    # 5: calibration against human-reviewed examples (milestone 8). A set fixes which versions
    # are to be labeled and how they were drawn; its items and human labels are immutable
    # (triggers refuse updates and deletes). No message text, notes or reviewer identity.
    """
    CREATE TABLE calibration_sets (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        sampling TEXT NOT NULL CHECK (sampling IN ('representative', 'prioritized')),
        seed INTEGER NOT NULL,
        primary_run_id INTEGER NOT NULL REFERENCES llm_runs (id),
        review_run_id INTEGER REFERENCES llm_runs (id),
        scoring_run_id INTEGER REFERENCES scoring_runs (id),
        population INTEGER NOT NULL,
        size INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        tool_version TEXT NOT NULL
    );

    CREATE TABLE calibration_items (
        set_id INTEGER NOT NULL REFERENCES calibration_sets (id),
        version_id INTEGER NOT NULL REFERENCES conversation_versions (id),
        stratum TEXT NOT NULL,
        PRIMARY KEY (set_id, version_id)
    ) WITHOUT ROWID;

    CREATE TABLE calibration_labels (
        set_id INTEGER NOT NULL,
        version_id INTEGER NOT NULL,
        label TEXT NOT NULL CHECK (label IN ('HIGH', 'MEDIUM', 'LOW', 'UNCLEAR')),
        labels_sha256 TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        PRIMARY KEY (set_id, version_id),
        FOREIGN KEY (set_id, version_id) REFERENCES calibration_items (set_id, version_id)
    ) WITHOUT ROWID;

    CREATE TRIGGER calibration_sets_immutable BEFORE UPDATE ON calibration_sets
    BEGIN SELECT RAISE(ABORT, 'calibration sets are immutable'); END;

    CREATE TRIGGER calibration_sets_no_delete BEFORE DELETE ON calibration_sets
    BEGIN SELECT RAISE(ABORT, 'calibration sets are immutable'); END;

    -- BEFORE INSERT triggers also stop INSERT OR REPLACE, which deletes the old row without
    -- firing delete triggers.
    CREATE TRIGGER calibration_sets_no_replace BEFORE INSERT ON calibration_sets
    WHEN EXISTS (SELECT 1 FROM calibration_sets WHERE id = NEW.id OR name = NEW.name)
    BEGIN SELECT RAISE(ABORT, 'calibration sets are immutable'); END;

    CREATE TRIGGER calibration_items_no_replace BEFORE INSERT ON calibration_items
    WHEN EXISTS (SELECT 1 FROM calibration_items
                 WHERE set_id = NEW.set_id AND version_id = NEW.version_id)
    BEGIN SELECT RAISE(ABORT, 'calibration items are immutable'); END;

    -- a set holds exactly `size` items: once complete, no item can be added
    CREATE TRIGGER calibration_items_closed BEFORE INSERT ON calibration_items
    WHEN (SELECT count(*) FROM calibration_items WHERE set_id = NEW.set_id)
         >= coalesce((SELECT size FROM calibration_sets WHERE id = NEW.set_id), 0)
    BEGIN SELECT RAISE(ABORT, 'calibration set is complete'); END;

    CREATE TRIGGER calibration_labels_no_replace BEFORE INSERT ON calibration_labels
    WHEN EXISTS (SELECT 1 FROM calibration_labels
                 WHERE set_id = NEW.set_id AND version_id = NEW.version_id)
    BEGIN SELECT RAISE(ABORT, 'calibration labels are immutable'); END;

    CREATE TRIGGER calibration_items_immutable BEFORE UPDATE ON calibration_items
    BEGIN SELECT RAISE(ABORT, 'calibration items are immutable'); END;

    CREATE TRIGGER calibration_items_no_delete BEFORE DELETE ON calibration_items
    BEGIN SELECT RAISE(ABORT, 'calibration items are immutable'); END;

    CREATE TRIGGER calibration_labels_immutable BEFORE UPDATE ON calibration_labels
    BEGIN SELECT RAISE(ABORT, 'calibration labels are immutable'); END;

    CREATE TRIGGER calibration_labels_no_delete BEFORE DELETE ON calibration_labels
    BEGIN SELECT RAISE(ABORT, 'calibration labels are immutable'); END;
    """,
)

LATEST_VERSION = len(MIGRATIONS)
