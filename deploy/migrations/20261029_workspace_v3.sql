-- Additive and idempotent on SQLite/MySQL. Application startup runs these DDLs.
-- Preserve the existing account-sync.key when moving or restoring the database.
CREATE TABLE IF NOT EXISTS chat_conversations (
    user_id VARCHAR(64) NOT NULL, id VARCHAR(64) NOT NULL,
    version INTEGER NOT NULL, status VARCHAR(20) NOT NULL,
    created DOUBLE NOT NULL, updated DOUBLE NOT NULL,
    stored_size INTEGER NOT NULL, body LONGBLOB NOT NULL,
    PRIMARY KEY(user_id,id)
);
CREATE TABLE IF NOT EXISTS chat_stream_requests (
    user_id VARCHAR(64) NOT NULL, request_id VARCHAR(128) NOT NULL,
    conversation_id VARCHAR(64) NOT NULL, run_id VARCHAR(64) NOT NULL,
    fingerprint VARCHAR(64) NOT NULL, status VARCHAR(20) NOT NULL,
    created DOUBLE NOT NULL, updated DOUBLE NOT NULL,
    body LONGBLOB NOT NULL, PRIMARY KEY(user_id,request_id)
);
CREATE TABLE IF NOT EXISTS timetable_ai_lines (
    id VARCHAR(64) PRIMARY KEY, config LONGBLOB NOT NULL
);
