"""Project hierarchy and editable screenplay/storyboard persistence."""

from __future__ import annotations

import json
import math
import time

from .database import new_id


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def require_project(connection, workspace_id: str, project_id: str):
    row = connection.execute(
        "SELECT * FROM projects_v2 WHERE id=? AND workspace_id=? AND status!='deleted'",
        (project_id, workspace_id),
    ).fetchone()
    if not row:
        raise ValueError("项目不存在或无权访问")
    return row


def save_source(connection, user, workspace_id: str, project_id: str, body: dict) -> dict:
    require_project(connection, workspace_id, project_id)
    source_text = str(body.get("sourceText", ""))
    if len(source_text) > 200_000:
        raise ValueError("原文暂时不能超过 20 万字")
    now = int(time.time())
    with connection:
        connection.execute(
            """INSERT INTO project_sources_v2(
                project_id,source_type,source_text,visual_style,settings_json,analysis_json,
                revision,updated_by,updated_at
            ) VALUES(?,?,?,?,?,'{}',1,?,?)
            ON CONFLICT(project_id) DO UPDATE SET
                source_type=excluded.source_type,source_text=excluded.source_text,
                visual_style=excluded.visual_style,settings_json=excluded.settings_json,
                revision=project_sources_v2.revision+1,updated_by=excluded.updated_by,
                updated_at=excluded.updated_at""",
            (
                project_id, str(body.get("sourceType", "auto"))[:30], source_text,
                str(body.get("visualStyle", "电影感写实"))[:200],
                _dump(body.get("settings") or {}), user["id"], now,
            ),
        )
        connection.execute("UPDATE projects_v2 SET updated_at=? WHERE id=?", (now, project_id))
    return {"projectId": project_id, "saved": True, "updatedAt": now}


def _find_or_create_workspace_entity(connection, user, workspace_id: str, entity_type: str,
                                     name: str, metadata: dict, entity_id=None) -> str:
    # A matching name is not evidence of identity. Reuse must carry an explicit ID.
    # In particular, another project's description must never overwrite a shared character.
    if entity_id:
        row = connection.execute(
            """SELECT id FROM entities_v2 WHERE id=? AND workspace_id=?
               AND owner_user_id=? AND entity_type=?""",
            (entity_id, workspace_id, user["id"], entity_type),
        ).fetchone()
        if not row:
            raise ValueError("指定复用的对象不存在、类型不匹配或无权使用")
        return row["id"]
    now = int(time.time())
    entity_id = new_id(entity_type)
    connection.execute(
        """INSERT INTO entities_v2(
            id,workspace_id,owner_user_id,entity_type,parent_id,project_id,name,
            metadata_json,created_at,updated_at
        ) VALUES(?,?,?,?,NULL,NULL,?,?,?,?)""",
        (entity_id, workspace_id, user["id"], entity_type, name, _dump(metadata), now, now),
    )
    return entity_id


def apply_analysis(connection, user, workspace_id: str, project_id: str, analysis: dict) -> dict:
    if not isinstance(analysis, dict):
        raise ValueError("分析结果必须是对象")
    project = require_project(connection, workspace_id, project_id)
    characters = analysis.get("characters") or []
    locations = analysis.get("scenes") or analysis.get("locations") or []
    shots = analysis.get("shots") or []
    if not isinstance(characters, list) or not isinstance(locations, list) or not isinstance(shots, list):
        raise ValueError("分析结果结构不正确")
    if any(not isinstance(item, dict) for item in characters + locations + shots):
        raise ValueError("人物、场景和分镜条目必须是对象")
    if len(characters) > 100 or len(locations) > 200 or len(shots) > 500:
        raise ValueError("单次拆解最多 100 个人物、200 个场景和 500 个镜头，请分批处理")
    now = int(time.time())
    created = {"characters": [], "locations": [], "scenes": [], "shots": []}
    with connection:
        source = connection.execute(
            "SELECT analysis_json FROM project_sources_v2 WHERE project_id=?", (project_id,)
        ).fetchone()
        if not source:
            connection.execute(
                """INSERT INTO project_sources_v2(
                    project_id,source_type,source_text,visual_style,settings_json,analysis_json,
                    revision,updated_by,updated_at
                ) VALUES(?,'auto','','电影感写实','{}',?,1,?,?)""",
                (project_id, _dump(analysis), user["id"], now),
            )
        else:
            connection.execute(
                "UPDATE project_sources_v2 SET analysis_json=?,updated_by=?,updated_at=? WHERE project_id=?",
                (_dump(analysis), user["id"], now, project_id),
            )

        character_by_name = {}
        for item in characters[:100]:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            metadata = {
                "description": item.get("description", ""), "visualPrompt": item.get("visualPrompt", ""),
                "continuity": item.get("continuity", ""), "source": "story_analysis",
            }
            entity_id = _find_or_create_workspace_entity(
                connection, user, workspace_id, "character", name, metadata,
                item.get("entityId") or item.get("existingEntityId"),
            )
            character_by_name[name] = entity_id if name not in character_by_name else None
            created["characters"].append(entity_id)
            connection.execute(
                """INSERT OR IGNORE INTO entity_links_v2(
                    id,source_entity_id,target_type,target_id,role,created_by,created_at
                ) VALUES(?,?,'project',?,'project_use',?,?)""",
                (new_id("elink"), entity_id, project_id, user["id"], now),
            )

        location_by_source = {}
        for index, item in enumerate(locations[:200], 1):
            name = str(item.get("name", "")).strip() or f"场景 {index}"
            location_id = _find_or_create_workspace_entity(
                connection, user, workspace_id, "location", name,
                {"description": item.get("description", ""), "visualPrompt": item.get("visualPrompt", ""), "source": "story_analysis"},
                item.get("entityId") or item.get("existingEntityId"),
            )
            location_by_source[str(item.get("id") or index)] = location_id
            created["locations"].append(location_id)
            connection.execute(
                """INSERT OR IGNORE INTO entity_links_v2(
                    id,source_entity_id,target_type,target_id,role,created_by,created_at
                ) VALUES(?,?,'project',?,'project_use',?,?)""",
                (new_id("elink"), location_id, project_id, user["id"], now),
            )
            scene_id = new_id("scene")
            scene_meta = {
                "sourceKey": str(item.get("id") or index), "description": item.get("description", ""),
                "visualPrompt": item.get("visualPrompt", ""), "locationId": location_id,
                "time": item.get("time", ""), "weather": item.get("weather", ""),
                "order": index, "status": "draft", "source": "story_analysis",
            }
            connection.execute(
                """INSERT INTO entities_v2(
                    id,workspace_id,owner_user_id,entity_type,parent_id,project_id,name,
                    metadata_json,created_at,updated_at
                ) VALUES(?,?,?,'scene',NULL,?,?,?,?,?)""",
                (scene_id, workspace_id, user["id"], project_id, name, _dump(scene_meta), now, now),
            )
            created["scenes"].append(scene_id)
            location_by_source["scene:" + str(item.get("id") or index)] = scene_id

        for index, item in enumerate(shots[:500], 1):
            source_scene = str(item.get("sceneId", ""))
            scene_id = location_by_source.get("scene:" + source_scene)
            name = str(item.get("title", "")).strip() or f"镜头 {index:03d}"
            shot_characters = item.get("characters") or []
            if not isinstance(shot_characters, list) or any(not isinstance(name, str) for name in shot_characters):
                raise ValueError("分镜人物必须是名称列表")
            character_ids = [character_by_name[name] for name in shot_characters if character_by_name.get(name)]
            duration = float(item.get("duration") or 3)
            if not math.isfinite(duration) or not .5 <= duration <= 60:
                raise ValueError("镜头时长必须在 0.5 到 60 秒之间")
            metadata = {
                "number": index, "duration": duration,
                "shotSize": item.get("shotSize", ""), "camera": item.get("camera", ""),
                "angle": item.get("angle", ""), "lens": item.get("lens", ""),
                "movement": item.get("movement", item.get("camera", "")),
                "action": item.get("action", ""), "dialogue": item.get("dialogue", ""),
                "audio": item.get("audio", ""), "prompt": item.get("prompt", ""),
                "negativePrompt": item.get("negativePrompt", ""), "characterIds": character_ids,
                "status": "draft", "selectedVersionId": None, "reviewNote": "", "source": "story_analysis",
            }
            shot_id = new_id("shot")
            connection.execute(
                """INSERT INTO entities_v2(
                    id,workspace_id,owner_user_id,entity_type,parent_id,project_id,name,
                    metadata_json,created_at,updated_at
                ) VALUES(?,?,?,'shot',?,?,?,?,?,?)""",
                (shot_id, workspace_id, user["id"], scene_id, project_id, name, _dump(metadata), now, now),
            )
            created["shots"].append(shot_id)

        # Previous analysis-derived project scenes/shots stay recoverable but leave the active view.
        keep_ids = set(created["scenes"] + created["shots"])
        old_rows = connection.execute(
            "SELECT id,metadata_json FROM entities_v2 WHERE project_id=? AND entity_type IN ('scene','shot')",
            (project_id,),
        ).fetchall()
        for row in old_rows:
            if row["id"] in keep_ids:
                continue
            metadata = json.loads(row["metadata_json"] or "{}")
            if metadata.get("source") != "story_analysis":
                continue
            metadata["superseded"] = True
            connection.execute(
                "UPDATE entities_v2 SET metadata_json=?,updated_at=? WHERE id=?",
                (_dump(metadata), now, row["id"]),
            )
        connection.execute(
            "UPDATE projects_v2 SET name=?,description=?,aspect=?,updated_at=? WHERE id=?",
            (
                str(analysis.get("projectTitle") or project["name"])[:80],
                str(analysis.get("logline") or project["description"])[:1000],
                str(analysis.get("aspect") or project["aspect"]), now, project_id,
            ),
        )
    return {"projectId": project_id, "created": created, "analysis": analysis}


def update_entity(connection, user, workspace_id: str, entity_id: str, body: dict) -> dict:
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        "SELECT * FROM entities_v2 WHERE id=? AND workspace_id=?", (entity_id, workspace_id)
    ).fetchone()
    if not row:
        raise ValueError("对象不存在或无权修改")
    metadata = json.loads(row["metadata_json"] or "{}")
    patch = body.get("metadata") or {}
    if not isinstance(patch, dict):
        raise ValueError("metadata 必须是对象")
    metadata.update(patch)
    if row["entity_type"] == "shot":
        if "selectedVersionId" in patch:
            raise ValueError("请选择镜头候选版本，不可手工指定版本 ID")
        duration = metadata.get("duration", 3)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not .5 <= duration <= 60:
            raise ValueError("镜头时长必须在 0.5 到 60 秒之间")
        status = metadata.get("status", "draft")
        if status not in {"draft", "selected", "approved", "in_edit"}:
            raise ValueError("镜头状态不正确")
        selected = metadata.get("selectedVersionId")
        if status != "draft":
            version = connection.execute(
                """SELECT v.id FROM generation_versions_v2 v JOIN media_assets_v2 m ON m.id=v.media_id
                   WHERE v.id=? AND v.shot_id=? AND v.created_by=? AND m.workspace_id=?
                     AND m.lifecycle_state='active'""", (selected, entity_id, user["id"], workspace_id),
            ).fetchone()
            if not version:
                raise ValueError("请先在任务版本中选择有效结果，再修改审核状态")
        else:
            metadata["selectedVersionId"] = None
    name = str(body.get("name", row["name"])).strip()[:120]
    if not name:
        raise ValueError("名称不能为空")
    now = int(time.time())
    with connection:
        if row["entity_type"] == "shot":
            connection.execute(
                """UPDATE shot_version_states_v2 SET state='candidate',updated_by=?,updated_at=?
                   WHERE shot_id=? AND state IN ('selected','approved','in_edit')""",
                (user["id"], now, entity_id),
            )
            if metadata.get("selectedVersionId"):
                connection.execute(
                    """INSERT INTO shot_version_states_v2(shot_id,version_id,state,review_note,updated_by,updated_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(shot_id,version_id) DO UPDATE SET
                       state=excluded.state,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                    (entity_id, metadata["selectedVersionId"], metadata["status"], str(metadata.get("reviewNote", ""))[:2000], user["id"], now),
                )
        connection.execute(
            "UPDATE entities_v2 SET name=?,metadata_json=?,updated_at=? WHERE id=?",
            (name, _dump(metadata), now, entity_id),
        )
    return {"entity": {"id": entity_id, "type": row["entity_type"], "name": name, "metadata": metadata}}


def project_detail(connection, workspace_id: str, project_id: str) -> dict:
    project = require_project(connection, workspace_id, project_id)
    source = connection.execute("SELECT * FROM project_sources_v2 WHERE project_id=?", (project_id,)).fetchone()
    entities = connection.execute(
        """SELECT * FROM entities_v2 WHERE project_id=?
           ORDER BY entity_type,updated_at,created_at""", (project_id,),
    ).fetchall()
    grouped = {"episodes": [], "scenes": [], "shots": []}
    for row in entities:
        metadata = json.loads(row["metadata_json"] or "{}")
        if metadata.get("superseded"):
            continue
        item = {"id": row["id"], "name": row["name"], "parentId": row["parent_id"], "metadata": metadata}
        key = {"episode": "episodes", "scene": "scenes", "shot": "shots"}.get(row["entity_type"])
        if key:
            grouped[key].append(item)
    workspace_entities = connection.execute(
        """SELECT * FROM entities_v2 WHERE workspace_id=?
           AND entity_type IN ('character','character_look','location','scene_variant','theme')
           ORDER BY entity_type,name""", (workspace_id,),
    ).fetchall()
    reusable = {"characters": [], "looks": [], "locations": [], "variants": [], "themes": []}
    project_entity_ids = {
        row["source_entity_id"] for row in connection.execute(
            "SELECT source_entity_id FROM entity_links_v2 WHERE target_type='project' AND target_id=?",
            (project_id,),
        ).fetchall()
    }
    project_entity_ids.update(row["id"] for row in workspace_entities if row["project_id"] == project_id)
    key_map = {"character": "characters", "character_look": "looks", "location": "locations", "scene_variant": "variants", "theme": "themes"}
    for row in workspace_entities:
        reusable[key_map[row["entity_type"]]].append({
            "id": row["id"], "name": row["name"], "parentId": row["parent_id"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
        })
    tasks = connection.execute(
        """SELECT id,shot_id,kind,status,title,requested_count,submitted_job_ids_json,error,created_at,updated_at
           FROM generation_tasks_v2 WHERE project_id=? ORDER BY updated_at DESC LIMIT 100""", (project_id,),
    ).fetchall()
    return {
        "project": {
            "id": project["id"], "name": project["name"], "description": project["description"],
            "status": project["status"], "aspect": project["aspect"], "updatedAt": project["updated_at"],
        },
        "source": {
            "sourceType": source["source_type"], "sourceText": source["source_text"],
            "visualStyle": source["visual_style"], "settings": json.loads(source["settings_json"]),
            "analysis": json.loads(source["analysis_json"]), "revision": source["revision"],
        } if source else None,
        **grouped, **reusable,
        "projectEntityIds": sorted(project_entity_ids),
        "tasks": [{
            "id": row["id"], "shotId": row["shot_id"], "kind": row["kind"], "status": row["status"],
            "title": row["title"], "count": row["requested_count"],
            "jobIds": json.loads(row["submitted_job_ids_json"]), "error": row["error"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        } for row in tasks],
    }


def overview(connection, workspace_id: str) -> dict:
    projects = connection.execute(
        "SELECT * FROM projects_v2 WHERE workspace_id=? AND status='active' ORDER BY updated_at DESC",
        (workspace_id,),
    ).fetchall()
    result = []
    for row in projects:
        counts = connection.execute(
            """SELECT entity_type,COUNT(*) AS count FROM entities_v2
               WHERE project_id=? AND COALESCE(json_extract(metadata_json,'$.superseded'),0)<>1
               GROUP BY entity_type""", (row["id"],),
        ).fetchall()
        count_map = {item["entity_type"]: item["count"] for item in counts}
        task_counts = connection.execute(
            """SELECT status,COUNT(*) AS count FROM generation_tasks_v2
               WHERE project_id=? GROUP BY status""", (row["id"],),
        ).fetchall()
        result.append({
            "id": row["id"], "name": row["name"], "description": row["description"],
            "aspect": row["aspect"], "status": row["status"], "updatedAt": row["updated_at"],
            "counts": {**count_map, "tasks": {item["status"]: item["count"] for item in task_counts}},
        })
    return {"projects": result}
