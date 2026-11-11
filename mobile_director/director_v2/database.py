from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path


SCHEMA_VERSION = 2


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=15000")
    migrate(connection)
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations_v2(
            version INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workspaces_v2(
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(owner_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS workspace_members_v2(
            workspace_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('owner','manager','editor','viewer')),
            created_at INTEGER NOT NULL,
            PRIMARY KEY(workspace_id,user_id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces_v2(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS projects_v2(
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            aspect TEXT NOT NULL DEFAULT '16:9',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            archived_at INTEGER,
            FOREIGN KEY(workspace_id) REFERENCES workspaces_v2(id),
            FOREIGN KEY(owner_user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_projects_workspace_v2
            ON projects_v2(workspace_id,updated_at DESC);

        CREATE TABLE IF NOT EXISTS project_sources_v2(
            project_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL DEFAULT 'auto',
            source_text TEXT NOT NULL DEFAULT '',
            visual_style TEXT NOT NULL DEFAULT '电影感写实',
            settings_json TEXT NOT NULL DEFAULT '{}',
            analysis_json TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects_v2(id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS entities_v2(
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_user_id INTEGER NOT NULL,
            entity_type TEXT NOT NULL CHECK(entity_type IN (
                'character','character_look','location','scene_variant',
                'theme','episode','scene','shot'
            )),
            parent_id TEXT,
            project_id TEXT,
            name TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES workspaces_v2(id),
            FOREIGN KEY(owner_user_id) REFERENCES users(id),
            FOREIGN KEY(parent_id) REFERENCES entities_v2(id) ON DELETE SET NULL,
            FOREIGN KEY(project_id) REFERENCES projects_v2(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_entities_workspace_type_v2
            ON entities_v2(workspace_id,entity_type,updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_entities_project_type_v2
            ON entities_v2(project_id,entity_type,updated_at DESC);

        CREATE TABLE IF NOT EXISTS entity_links_v2(
            id TEXT PRIMARY KEY,
            source_entity_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(source_entity_id,target_type,target_id,role),
            FOREIGN KEY(source_entity_id) REFERENCES entities_v2(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_entity_links_target_v2
            ON entity_links_v2(target_type,target_id,role);

        CREATE TABLE IF NOT EXISTS media_assets_v2(
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            media_kind TEXT NOT NULL CHECK(media_kind IN ('image','video','audio','document')),
            storage_scope TEXT NOT NULL CHECK(storage_scope IN ('input','output','managed')),
            storage_path TEXT NOT NULL,
            sha256 TEXT,
            byte_size INTEGER NOT NULL DEFAULT 0,
            mime_type TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'upload',
            lifecycle_state TEXT NOT NULL DEFAULT 'active',
            retrieval_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            deleted_at INTEGER,
            UNIQUE(owner_user_id,storage_scope,storage_path),
            FOREIGN KEY(workspace_id) REFERENCES workspaces_v2(id),
            FOREIGN KEY(owner_user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_media_workspace_v2
            ON media_assets_v2(workspace_id,updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_media_kind_v2
            ON media_assets_v2(workspace_id,media_kind,updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_media_hash_v2
            ON media_assets_v2(sha256) WHERE sha256 IS NOT NULL;

        CREATE TABLE IF NOT EXISTS asset_links_v2(
            id TEXT PRIMARY KEY,
            media_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            role TEXT NOT NULL,
            classification_revision_id TEXT,
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(media_id,target_type,target_id,role),
            FOREIGN KEY(media_id) REFERENCES media_assets_v2(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_asset_links_target_v2
            ON asset_links_v2(target_type,target_id,role);

        CREATE TABLE IF NOT EXISTS generation_versions_v2(
            id TEXT PRIMARY KEY,
            media_id TEXT NOT NULL,
            parent_version_id TEXT,
            project_id TEXT,
            shot_id TEXT,
            prompt TEXT NOT NULL DEFAULT '',
            parameters_json TEXT NOT NULL DEFAULT '{}',
            input_media_json TEXT NOT NULL DEFAULT '[]',
            legacy_job_prefix TEXT,
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(media_id) REFERENCES media_assets_v2(id),
            FOREIGN KEY(parent_version_id) REFERENCES generation_versions_v2(id),
            FOREIGN KEY(project_id) REFERENCES projects_v2(id),
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS generation_tasks_v2(
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            project_id TEXT,
            shot_id TEXT,
            kind TEXT NOT NULL CHECK(kind IN ('image','image_edit','video','storyboard','classification')),
            status TEXT NOT NULL CHECK(status IN ('draft','queued','running','completed','failed','cancelled')),
            title TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            parameters_json TEXT NOT NULL DEFAULT '{}',
            input_media_json TEXT NOT NULL DEFAULT '[]',
            requested_count INTEGER NOT NULL DEFAULT 1,
            submitted_job_ids_json TEXT NOT NULL DEFAULT '[]',
            error TEXT NOT NULL DEFAULT '',
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES workspaces_v2(id),
            FOREIGN KEY(project_id) REFERENCES projects_v2(id) ON DELETE SET NULL,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_generation_tasks_workspace_v2
            ON generation_tasks_v2(workspace_id,updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_generation_tasks_shot_v2
            ON generation_tasks_v2(shot_id,updated_at DESC);

        CREATE TABLE IF NOT EXISTS shot_version_states_v2(
            shot_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('candidate','selected','approved','in_edit','rejected')),
            review_note TEXT NOT NULL DEFAULT '',
            updated_by INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(shot_id,version_id),
            FOREIGN KEY(version_id) REFERENCES generation_versions_v2(id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS deliveries_v2(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            media_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects_v2(id) ON DELETE CASCADE,
            FOREIGN KEY(media_id) REFERENCES media_assets_v2(id),
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS classification_jobs_v2(
            id TEXT PRIMARY KEY,
            media_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('processing','draft','archived','failed')),
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(media_id) REFERENCES media_assets_v2(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS classification_revisions_v2(
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            media_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('draft','archived')),
            data_json TEXT NOT NULL,
            classifier_version TEXT NOT NULL DEFAULT '1',
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            archived_at INTEGER,
            FOREIGN KEY(job_id) REFERENCES classification_jobs_v2(id),
            FOREIGN KEY(media_id) REFERENCES media_assets_v2(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_classification_media_v2
            ON classification_revisions_v2(media_id,created_at DESC);

        CREATE TABLE IF NOT EXISTS shares_v2(
            id TEXT PRIMARY KEY,
            media_id TEXT NOT NULL,
            owner_user_id INTEGER NOT NULL,
            visibility TEXT NOT NULL CHECK(visibility IN ('private','public','withdrawn')),
            title TEXT NOT NULL,
            classification_revision_id TEXT NOT NULL,
            permissions_json TEXT NOT NULL,
            recipe_json TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            withdrawn_at INTEGER,
            FOREIGN KEY(media_id) REFERENCES media_assets_v2(id),
            FOREIGN KEY(owner_user_id) REFERENCES users(id),
            FOREIGN KEY(classification_revision_id) REFERENCES classification_revisions_v2(id)
        );

        CREATE INDEX IF NOT EXISTS idx_public_shares_v2
            ON shares_v2(visibility,updated_at DESC);

        CREATE TABLE IF NOT EXISTS favorites_v2(
            user_id INTEGER NOT NULL,
            share_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(user_id,share_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(share_id) REFERENCES shares_v2(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS share_reports_v2(
            id TEXT PRIMARY KEY,
            share_id TEXT NOT NULL,
            reporter_user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            created_at INTEGER NOT NULL,
            UNIQUE(share_id,reporter_user_id),
            FOREIGN KEY(share_id) REFERENCES shares_v2(id) ON DELETE CASCADE,
            FOREIGN KEY(reporter_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS media_embeddings_v2(
            media_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            source_revision_id TEXT,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(media_id,model),
            FOREIGN KEY(media_id) REFERENCES media_assets_v2(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catalog_state_v2(
            workspace_id TEXT PRIMARY KEY,
            last_scan_at INTEGER NOT NULL DEFAULT 0,
            last_scan_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(workspace_id) REFERENCES workspaces_v2(id) ON DELETE CASCADE
        );
        """
    )
    # Serialize additive migration so concurrent first requests cannot race ALTER TABLE.
    connection.execute("BEGIN IMMEDIATE")
    if "tags_json" not in {row[1] for row in connection.execute("PRAGMA table_info(media_assets_v2)")}:
        connection.execute("ALTER TABLE media_assets_v2 ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations_v2(version,applied_at) VALUES(?,?)",
        (SCHEMA_VERSION, int(time.time())),
    )
    connection.commit()


def ensure_personal_workspace(connection: sqlite3.Connection, user) -> sqlite3.Row:
    row = connection.execute(
        """SELECT w.* FROM workspaces_v2 w
           JOIN workspace_members_v2 m ON m.workspace_id=w.id
           WHERE m.user_id=? AND m.role='owner'
           ORDER BY w.created_at LIMIT 1""",
        (user["id"],),
    ).fetchone()
    if row:
        return row
    now = int(time.time())
    workspace_id = "ws_" + uuid.uuid4().hex
    connection.execute(
        "INSERT INTO workspaces_v2 VALUES(?,?,?,?,?)",
        (workspace_id, user["id"], f"{user['username']} 的工作空间", now, now),
    )
    connection.execute(
        "INSERT INTO workspace_members_v2 VALUES(?,?,?,?)",
        (workspace_id, user["id"], "owner", now),
    )
    connection.commit()
    return connection.execute(
        "SELECT * FROM workspaces_v2 WHERE id=?", (workspace_id,)
    ).fetchone()


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex
