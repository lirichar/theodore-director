"""Persistent generation tasks and shot output version state."""

from __future__ import annotations

import json
import time
from urllib.parse import quote

from .database import new_id


def _require_media(connection, workspace_id, user_id, media_id, *, active=True):
    row = connection.execute(
        "SELECT * FROM media_assets_v2 WHERE id=? AND workspace_id=? AND owner_user_id=?",
        (media_id, workspace_id, user_id),
    ).fetchone()
    if not row or (active and row["lifecycle_state"] != "active"):
        raise ValueError("媒体不存在、已移入回收站或无权使用")
    return row


def validate_task_context(connection, user, workspace_id, *, project_id=None,
                          shot_id=None, input_media=None, parent_version_id=None,
                          entity_id=None, entity_type=None):
    """Check references before submitting any paid or local provider job."""
    if not connection.execute(
        """SELECT 1 FROM workspace_members_v2 WHERE workspace_id=? AND user_id=?
           AND role IN ('owner','manager','editor')""", (workspace_id, user["id"]),
    ).fetchone():
        raise ValueError("无权在该工作空间创建任务")
    if project_id and not connection.execute(
        "SELECT 1 FROM projects_v2 WHERE id=? AND workspace_id=? AND archived_at IS NULL",
        (project_id, workspace_id),
    ).fetchone():
        raise ValueError("项目不存在、已归档或无权访问")
    if shot_id:
        shot = connection.execute(
            "SELECT project_id,metadata_json FROM entities_v2 WHERE id=? AND workspace_id=? AND entity_type='shot'",
            (shot_id, workspace_id),
        ).fetchone()
        if not shot or not project_id or shot["project_id"] != project_id:
            raise ValueError("镜头不属于当前项目")
        if json.loads(shot["metadata_json"] or "{}").get("superseded"):
            raise ValueError("镜头已被新版分镜替代")
    if input_media is not None:
        if not isinstance(input_media, list) or len(input_media) > 64:
            raise ValueError("参考素材必须是最多 64 个媒体 ID 的列表")
        for media_id in input_media:
            if not isinstance(media_id, str) or not media_id:
                raise ValueError("参考素材 ID 不正确")
            _require_media(connection, workspace_id, user["id"], media_id)
    if parent_version_id:
        parent = connection.execute(
            "SELECT media_id FROM generation_versions_v2 WHERE id=? AND created_by=?",
            (parent_version_id, user["id"]),
        ).fetchone()
        if not parent:
            raise ValueError("来源版本不存在或无权使用")
        _require_media(connection, workspace_id, user["id"], parent["media_id"])
    if entity_id:
        if entity_type not in {"character", "character_look", "location", "scene_variant", "theme"}:
            raise ValueError("生成结果的关联对象类型不正确")
        if not connection.execute(
            "SELECT 1 FROM entities_v2 WHERE id=? AND workspace_id=? AND owner_user_id=? AND entity_type=?",
            (entity_id, workspace_id, user["id"], entity_type),
        ).fetchone():
            raise ValueError("生成结果的关联对象不存在或无权使用")


def create_task(connection, user, workspace_id: str, *, kind: str, title: str,
                prompt: str, parameters: dict, input_media: list, count: int,
                project_id: str | None = None, shot_id: str | None = None,
                provider: str = "", model: str = "") -> dict:
    if kind not in {"image", "image_edit", "video", "storyboard", "classification"}:
        raise ValueError("任务类型不正确")
    if not isinstance(parameters, dict):
        raise ValueError("任务参数必须是对象")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 16:
        raise ValueError("生成数量必须为 1 到 16 的整数")
    validate_task_context(
        connection, user, workspace_id, project_id=project_id, shot_id=shot_id,
        input_media=input_media, parent_version_id=parameters.get("parentVersionId"),
        entity_id=parameters.get("entityId"), entity_type=parameters.get("entityType"),
    )
    now = int(time.time())
    task_id = new_id("task")
    with connection:
        connection.execute(
            """INSERT INTO generation_tasks_v2(
                id,workspace_id,project_id,shot_id,kind,status,title,provider,model,prompt,
                parameters_json,input_media_json,requested_count,submitted_job_ids_json,error,
                created_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,'draft',?,?,?,?,?,?,?,'[]','',?,?,?)""",
            (task_id, workspace_id, project_id, shot_id, kind, title[:120], provider[:80],
             model[:120], prompt, json.dumps(parameters, ensure_ascii=False),
             json.dumps(input_media, ensure_ascii=False), count, user["id"], now, now),
        )
    return {"id": task_id, "status": "draft", "kind": kind, "count": count}


def _output_count(connection, task_id):
    return connection.execute(
        "SELECT COUNT(*) FROM generation_versions_v2 WHERE json_extract(parameters_json,'$.taskId')=?",
        (task_id,),
    ).fetchone()[0]


def mark_task(connection, task_id: str, user_id: int, status: str, *,
              job_ids=None, error: str = "", parameters_patch: dict | None = None) -> dict:
    transitions = {
        "draft": {"draft", "queued", "running", "failed", "cancelled"},
        "queued": {"queued", "running", "completed", "failed", "cancelled"},
        "running": {"running", "completed", "failed", "cancelled"},
        "completed": {"completed"}, "failed": {"failed"}, "cancelled": {"cancelled"},
    }
    if status not in transitions:
        raise ValueError("任务状态不正确")
    if job_ids is not None and (not isinstance(job_ids, list) or any(
        not isinstance(item, str) or not item for item in job_ids
    )):
        raise ValueError("队列任务 ID 不正确")
    if parameters_patch is not None and not isinstance(parameters_patch, dict):
        raise ValueError("任务参数补丁必须是对象")
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    with connection:
        row = connection.execute(
            "SELECT * FROM generation_tasks_v2 WHERE id=? AND created_by=?", (task_id, user_id)
        ).fetchone()
        if not row:
            raise ValueError("任务不存在或无权操作")
        if status not in transitions[row["status"]]:
            raise ValueError("任务状态不可回退；重新生成请创建新任务")
        count = _output_count(connection, task_id)
        if status == "completed" and count < row["requested_count"]:
            raise ValueError("结果数量不足，不能将任务标记为完成")
        parameters = json.loads(row["parameters_json"] or "{}")
        if parameters_patch:
            if row["status"] in {"completed", "failed", "cancelled"}:
                raise ValueError("已结束任务的生成参数不可修改")
            if set(parameters_patch) - {"prefix", "prefixes", "parentVersionId"}:
                raise ValueError("只能补充任务提交信息；调整生成参数请创建新任务")
            if parameters_patch.get("parentVersionId"):
                validate_task_context(
                    connection, {"id": user_id}, row["workspace_id"],
                    parent_version_id=parameters_patch["parentVersionId"],
                )
            parameters.update(parameters_patch)
        saved_job_ids = job_ids if job_ids is not None else json.loads(row["submitted_job_ids_json"])
        now = int(time.time())
        connection.execute(
            """UPDATE generation_tasks_v2 SET status=?,submitted_job_ids_json=?,error=?,
               parameters_json=?,updated_at=? WHERE id=?""",
            (status, json.dumps(saved_job_ids), error[:2000],
             json.dumps(parameters, ensure_ascii=False), now, task_id),
        )
    return {"id": task_id, "status": status, "jobIds": saved_job_ids, "error": error[:2000],
            "completedCount": count}


def task_detail(connection, workspace_id: str, task_id: str) -> dict:
    row = connection.execute(
        "SELECT * FROM generation_tasks_v2 WHERE id=? AND workspace_id=?", (task_id, workspace_id)
    ).fetchone()
    if not row:
        raise ValueError("任务不存在")
    versions = connection.execute(
        """SELECT v.*,m.display_name,m.media_kind,m.storage_scope,m.storage_path,m.retrieval_json
           FROM generation_versions_v2 v JOIN media_assets_v2 m ON m.id=v.media_id
           WHERE json_extract(v.parameters_json,'$.taskId')=? AND m.workspace_id=?
           ORDER BY v.created_at DESC,v.rowid DESC""", (task_id, workspace_id),
    ).fetchall()
    return {
        "id": row["id"], "projectId": row["project_id"], "shotId": row["shot_id"],
        "kind": row["kind"], "status": row["status"], "title": row["title"],
        "provider": row["provider"], "model": row["model"], "prompt": row["prompt"],
        "parameters": json.loads(row["parameters_json"]), "inputMedia": json.loads(row["input_media_json"]),
        "count": row["requested_count"], "completedCount": len(versions),
        "jobIds": json.loads(row["submitted_job_ids_json"]),
        "error": row["error"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "versions": [{"id": item["id"], "mediaId": item["media_id"], "name": item["display_name"],
                      "kind": item["media_kind"], "parentVersionId": item["parent_version_id"],
                      "url": f"/api/media/{item['storage_scope']}/{quote(item['storage_path'], safe='/')}",
                      "preview": (f"/api/thumb/{item['storage_scope']}/{quote(item['storage_path'], safe='/')}" if item["media_kind"] == "image" else f"/api/vthumb/{item['storage_scope']}/{quote(item['storage_path'], safe='/')}" if item["media_kind"] == "video" else None),
                      "createdAt": item["created_at"]} for item in versions],
    }


def register_version(connection, user, workspace_id: str, *, task_id: str, media_id: str,
                     parent_version_id: str | None = None) -> dict:
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    with connection:
        task = connection.execute(
            "SELECT * FROM generation_tasks_v2 WHERE id=? AND workspace_id=? AND created_by=?",
            (task_id, workspace_id, user["id"]),
        ).fetchone()
        if not task:
            raise ValueError("任务不存在或无权操作")
        media = _require_media(connection, workspace_id, user["id"], media_id)
        params = json.loads(task["parameters_json"])
        parent_version_id = parent_version_id or params.get("parentVersionId")
        if parent_version_id:
            validate_task_context(connection, user, workspace_id, parent_version_id=parent_version_id)
        expected_kind = {"image": "image", "image_edit": "image", "video": "video"}.get(task["kind"])
        if expected_kind and media["media_kind"] != expected_kind:
            raise ValueError("输出媒体类型与任务不匹配")
        if media_id in json.loads(task["input_media_json"]):
            raise ValueError("输入素材不能登记为本次生成结果")
        existing = connection.execute(
            "SELECT id,parameters_json FROM generation_versions_v2 WHERE media_id=?", (media_id,)
        ).fetchone()
        if existing:
            if json.loads(existing["parameters_json"]).get("taskId") != task_id:
                raise ValueError("该素材已经属于另一生成任务；复用请建立素材关联")
            return {"versionId": existing["id"], "existing": True,
                    "status": task["status"], "completedCount": _output_count(connection, task_id)}
        if task["status"] == "completed":
            raise ValueError("任务已完成，不能额外登记结果")
        version_id = new_id("version")
        now = int(time.time())
        params["taskId"] = task_id
        connection.execute(
            """INSERT INTO generation_versions_v2(
                id,media_id,parent_version_id,project_id,shot_id,prompt,parameters_json,
                input_media_json,legacy_job_prefix,created_by,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (version_id, media_id, parent_version_id, task["project_id"], task["shot_id"],
             task["prompt"], json.dumps(params, ensure_ascii=False), task["input_media_json"],
                params.get("prefix", ""), user["id"], now),
        )
        if task["shot_id"]:
            connection.execute(
                """INSERT OR IGNORE INTO shot_version_states_v2(
                    shot_id,version_id,state,review_note,updated_by,updated_at
                ) VALUES(?,?,'candidate','',?,?)""", (task["shot_id"], version_id, user["id"], now),
            )
        for target_type, target_id in (("project", task["project_id"]), ("shot", task["shot_id"])):
            if target_id:
                connection.execute(
                    """INSERT OR IGNORE INTO asset_links_v2(
                        id,media_id,target_type,target_id,role,created_by,created_at
                    ) VALUES(?,?,?,?, 'generated_result',?,?)""",
                    (new_id("link"), media_id, target_type, target_id, user["id"], now),
                )
        if params.get("entityId"):
            validate_task_context(
                connection, user, workspace_id, entity_id=params["entityId"],
                entity_type=params.get("entityType"),
            )
            connection.execute(
                """INSERT OR IGNORE INTO asset_links_v2(
                    id,media_id,target_type,target_id,role,created_by,created_at
                ) VALUES(?,?,?,?, 'reference',?,?)""",
                (new_id("link"), media_id, params["entityType"], params["entityId"], user["id"], now),
            )
        completed_count = _output_count(connection, task_id)
        # Retain late output after cancellation/failure, without reviving the task.
        status = task["status"] if task["status"] in {"failed", "cancelled"} else (
            "completed" if completed_count >= task["requested_count"] else "running"
        )
        connection.execute(
            "UPDATE generation_tasks_v2 SET status=?,updated_at=? WHERE id=?", (status, now, task_id),
        )
        connection.execute(
            "UPDATE media_assets_v2 SET source_kind='generation',updated_at=? WHERE id=?", (now, media_id),
        )
    return {"versionId": version_id, "existing": False, "status": status, "completedCount": completed_count}


def set_version_state(connection, user, workspace_id: str, shot_id: str, version_id: str,
                      state: str, note: str = "") -> dict:
    if state not in {"candidate", "selected", "approved", "in_edit", "rejected"}:
        raise ValueError("版本状态不正确")
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    with connection:
        shot = connection.execute(
            """SELECT metadata_json FROM entities_v2 WHERE id=? AND workspace_id=?
               AND owner_user_id=? AND entity_type='shot'""", (shot_id, workspace_id, user["id"]),
        ).fetchone()
        version = connection.execute(
            """SELECT v.id FROM generation_versions_v2 v JOIN media_assets_v2 m ON m.id=v.media_id
               WHERE v.id=? AND v.shot_id=? AND v.created_by=? AND m.workspace_id=?
               AND m.owner_user_id=? AND m.lifecycle_state='active'""",
            (version_id, shot_id, user["id"], workspace_id, user["id"]),
        ).fetchone()
        if not shot or not version:
            raise ValueError("镜头版本不存在或无权操作")
        now = int(time.time())
        metadata = json.loads(shot["metadata_json"] or "{}")
        if state in {"selected", "approved", "in_edit"}:
            metadata.update(selectedVersionId=version_id, status=state, reviewNote=note[:2000])
            connection.execute(
                """UPDATE shot_version_states_v2 SET state='candidate',updated_by=?,updated_at=?
                   WHERE shot_id=? AND version_id<>? AND state IN ('selected','approved','in_edit')""",
                (user["id"], now, shot_id, version_id),
            )
        elif metadata.get("selectedVersionId") == version_id:
            metadata.update(selectedVersionId=None, status="draft", reviewNote=note[:2000])
        connection.execute(
            """INSERT INTO shot_version_states_v2(
                shot_id,version_id,state,review_note,updated_by,updated_at
            ) VALUES(?,?,?,?,?,?) ON CONFLICT(shot_id,version_id) DO UPDATE SET
              state=excluded.state,review_note=excluded.review_note,
              updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (shot_id, version_id, state, note[:2000], user["id"], now),
        )
        connection.execute(
            "UPDATE entities_v2 SET metadata_json=?,updated_at=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), now, shot_id),
        )
    return {"shotId": shot_id, "versionId": version_id, "state": state, "reviewNote": note[:2000]}
