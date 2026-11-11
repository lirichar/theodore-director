"""Classification draft lifecycle: direct result, user edit, confirm and archive."""

from __future__ import annotations

import json
import time

from .database import new_id


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _validate_data(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("分类结果必须是对象")
    try:
        result = json.loads(_dump(data))
    except (TypeError, ValueError) as exc:
        raise ValueError("分类结果必须是有效 JSON，不能包含非有限数字") from exc
    descriptor = result.setdefault("retrievalDescriptor", {})
    links = result.setdefault("links", [])
    if not isinstance(descriptor, dict) or not isinstance(links, list):
        raise ValueError("分类数据格式不正确")
    for field in ("displayLabel", "searchCaption", "usageNotes"):
        descriptor.setdefault(field, "")
        if not isinstance(descriptor[field], str):
            raise ValueError("检索描述的文字字段必须是文本")
    for field in ("bestFor", "avoidFor", "keywords"):
        descriptor.setdefault(field, [])
        if not isinstance(descriptor[field], list) or any(not isinstance(item, str) for item in descriptor[field]):
            raise ValueError("检索描述的标签字段必须是文本列表")
    if any(not isinstance(link, dict) for link in links):
        raise ValueError("分类关联格式不正确")
    return result


def create_draft(connection, user, workspace_id: str, media_id: str, data: dict,
                 provider: str = "manual", model: str = "") -> dict:
    data = _validate_data(data)
    media = connection.execute(
        "SELECT id FROM media_assets_v2 WHERE id=? AND workspace_id=? AND owner_user_id=? AND lifecycle_state='active'",
        (media_id, workspace_id, user["id"]),
    ).fetchone()
    if not media:
        raise ValueError("媒体不存在或无权操作")
    now = int(time.time())
    job_id = new_id("clsjob")
    revision_id = new_id("clsrev")
    with connection:
        connection.execute(
            """INSERT INTO classification_jobs_v2(
                id,media_id,status,provider,model,error,created_by,created_at,updated_at
            ) VALUES(?,?,'draft',?,?,?, ?,?,?)""",
            (job_id, media_id, provider[:80], model[:120], "", user["id"], now, now),
        )
        connection.execute(
            """INSERT INTO classification_revisions_v2(
                id,job_id,media_id,state,data_json,classifier_version,created_by,created_at,archived_at
            ) VALUES(?,?,?,'draft',?,'1',?,?,NULL)""",
            (revision_id, job_id, media_id, _dump(data), user["id"], now),
        )
    return {
        "jobId": job_id,
        "revisionId": revision_id,
        "mediaId": media_id,
        "status": "draft",
        "classification": data,
    }


def update_draft(connection, user, revision_id: str, data: dict) -> dict:
    data = _validate_data(data)
    now = int(time.time())
    data["editedBy"] = user["id"]
    data["editedAt"] = now
    with connection:
        cursor = connection.execute(
            """UPDATE classification_revisions_v2 SET data_json=?
               WHERE id=? AND created_by=? AND state='draft'
                 AND EXISTS(SELECT 1 FROM media_assets_v2 m
                            WHERE m.id=classification_revisions_v2.media_id
                              AND m.owner_user_id=? AND m.lifecycle_state='active')""",
            (_dump(data), revision_id, user["id"], user["id"]),
        )
        if not cursor.rowcount:
            raise ValueError("分类草稿不存在、无权操作或已归档版本不可修改")
        row = connection.execute(
            "SELECT media_id,job_id FROM classification_revisions_v2 WHERE id=?", (revision_id,),
        ).fetchone()
        connection.execute(
            "UPDATE classification_jobs_v2 SET updated_at=? WHERE id=?",
            (now, row["job_id"]),
        )
    return {
        "revisionId": revision_id,
        "mediaId": row["media_id"],
        "status": "draft",
        "classification": data,
    }


def confirm_archive(connection, user, workspace_id: str, revision_id: str,
                    edited_data: dict | None = None) -> dict:
    now = int(time.time())
    with connection:
        # The conditional write claims this draft under SQLite's write lock.
        # An edit/confirmation racing this request cannot overwrite its archive.
        cursor = connection.execute(
            """UPDATE classification_revisions_v2 SET state='archived',archived_at=?
               WHERE id=? AND created_by=? AND state='draft'
                 AND EXISTS(SELECT 1 FROM media_assets_v2 m
                            WHERE m.id=classification_revisions_v2.media_id
                              AND m.workspace_id=? AND m.owner_user_id=?
                              AND m.lifecycle_state='active')""",
            (now, revision_id, user["id"], workspace_id, user["id"]),
        )
        if not cursor.rowcount:
            raise ValueError("分类草稿不存在、无权操作或已归档版本不可修改")
        row = connection.execute(
            "SELECT * FROM classification_revisions_v2 WHERE id=?", (revision_id,),
        ).fetchone()
        data = _validate_data(edited_data if edited_data is not None else json.loads(row["data_json"]))
        if edited_data is not None:
            data["editedBy"] = user["id"]
            data["editedAt"] = now
        descriptor = data["retrievalDescriptor"]
        checked_links = set()
        valid_types = {
            "character", "character_look", "location", "scene_variant",
            "theme", "episode", "scene", "shot",
        }
        for link in data["links"]:
            target_type = str(link.get("targetType", ""))
            target_id = str(link.get("targetId", ""))
            role = str(link.get("role", "classification")).strip()[:64] or "classification"
            if target_type not in valid_types:
                raise ValueError("分类关联类型不正确")
            target = connection.execute(
                "SELECT id FROM entities_v2 WHERE id=? AND workspace_id=? AND entity_type=?",
                (target_id, workspace_id, target_type),
            ).fetchone()
            if not target:
                raise ValueError("分类关联对象不存在")
            checked_links.add((target_type, target_id, role))
        connection.execute(
            "UPDATE classification_revisions_v2 SET data_json=? WHERE id=?",
            (_dump(data), revision_id),
        )
        connection.execute(
            "UPDATE classification_jobs_v2 SET status='archived',updated_at=? WHERE id=?",
            (now, row["job_id"]),
        )
        connection.execute(
            "UPDATE media_assets_v2 SET retrieval_json=?,updated_at=? WHERE id=?",
            (_dump(descriptor), now, row["media_id"]),
        )
        connection.execute(
            "DELETE FROM asset_links_v2 WHERE media_id=? AND classification_revision_id IS NOT NULL",
            (row["media_id"],),
        )
        for target_type, target_id, role in sorted(checked_links):
            connection.execute(
                """INSERT INTO asset_links_v2(
                    id,media_id,target_type,target_id,role,classification_revision_id,created_by,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(media_id,target_type,target_id,role) DO NOTHING""",
                (new_id("link"), row["media_id"], target_type, target_id, role,
                 revision_id, user["id"], now),
            )
    return {
        "revisionId": revision_id,
        "mediaId": row["media_id"],
        "status": "archived",
        "classification": data,
    }


def list_revisions(connection, user, media_id: str) -> list[dict]:
    rows = connection.execute(
        """SELECT id,job_id,state,data_json,created_at,archived_at
           FROM classification_revisions_v2
           WHERE media_id=? AND created_by=? ORDER BY created_at DESC,rowid DESC""",
        (media_id, user["id"]),
    ).fetchall()
    return [
        {
            "revisionId": row["id"],
            "jobId": row["job_id"],
            "status": row["state"],
            "classification": json.loads(row["data_json"]),
            "createdAt": row["created_at"],
            "archivedAt": row["archived_at"],
        }
        for row in rows
    ]
