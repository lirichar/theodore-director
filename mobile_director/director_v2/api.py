"""Side-by-side v2 API contract delegated to by the legacy HTTP server."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs

from .catalog import compute_hash, link_media, list_media, permanent_delete, promote_without_copy, require_owned_media, serialize_media, set_lifecycle, sync_legacy_catalog, unlink_media, update_display_name
from .classification import confirm_archive, create_draft, list_revisions, update_draft
from .catalog import update_tags
from .database import connect, ensure_personal_workspace, new_id
from .sharing import public_listing, public_media_row, publish, remix_draft, report_share, toggle_favorite, withdraw
from .projects import apply_analysis, overview, project_detail, require_project, save_source, update_entity
from .tasks import create_task, mark_task, register_version, set_version_state, task_detail


ENTITY_TYPES = {
    "character", "character_look", "location", "scene_variant",
    "theme", "episode", "scene", "shot",
}


class DirectorV2API:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _context(self, user):
        connection = connect(self.db_path)
        workspace = ensure_personal_workspace(connection, user)
        return connection, workspace

    def classification_context(self, user, media_id: str, *, input_root: Path,
                               output_root: Path) -> dict:
        """Resolve an owned canonical media file plus reusable entity vocabulary."""
        connection, workspace = self._context(user)
        try:
            row = require_owned_media(connection, media_id, user["id"])
            root = Path(input_root) if row["storage_scope"] == "input" else Path(output_root)
            path = (root / row["storage_path"]).resolve()
            if root.resolve() not in path.parents or not path.is_file():
                raise ValueError("媒体文件不存在")
            return {
                "path": path,
                "media": serialize_media(row),
                "entities": self._entities(connection, workspace["id"]),
            }
        finally:
            connection.close()

    def public_get(self, path: str, query_string: str = "") -> tuple[int, dict]:
        connection = connect(self.db_path)
        try:
            query = parse_qs(query_string)
            if path == "/api/v2/discovery":
                return 200, public_listing(
                    connection, (query.get("q") or [""])[0],
                    int((query.get("limit") or ["48"])[0]),
                    int((query.get("offset") or ["0"])[0]),
                )
            return 404, {"error": "public_route_not_found"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        finally:
            connection.close()

    def public_preview_context(self, share_id: str, *, input_root_for_user,
                               output_root_for_user) -> dict:
        connection = connect(self.db_path)
        try:
            row = public_media_row(connection, share_id)
            if not row:
                raise ValueError("公开素材不存在或已撤回")
            owner = connection.execute("SELECT id,username,role FROM users WHERE id=?", (row["owner_user_id"],)).fetchone()
            root = Path(input_root_for_user(owner)) if row["storage_scope"] == "input" else Path(output_root_for_user(owner))
            path = (root / row["storage_path"]).resolve()
            if root.resolve() not in path.parents or not path.is_file():
                raise ValueError("公开预览不存在")
            return {"path": path, "kind": row["media_kind"]}
        finally:
            connection.close()

    @staticmethod
    def _projects(connection, workspace_id: str) -> list[dict]:
        rows = connection.execute(
            """SELECT id,name,description,status,aspect,created_at,updated_at
               FROM projects_v2 WHERE workspace_id=? AND status!='deleted' ORDER BY updated_at DESC""",
            (workspace_id,),
        ).fetchall()
        return [
            {
                "id": row["id"], "name": row["name"], "description": row["description"],
                "status": row["status"], "aspect": row["aspect"],
                "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _entities(connection, workspace_id: str, entity_type: str = "",
                  project_id: str = "") -> list[dict]:
        clauses = ["workspace_id=?"]
        params = [workspace_id]
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        rows = connection.execute(
            f"""SELECT id,entity_type,parent_id,project_id,name,metadata_json,created_at,updated_at
                FROM entities_v2 WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC""",
            params,
        ).fetchall()
        return [
            {
                "id": row["id"], "type": row["entity_type"], "parentId": row["parent_id"],
                "projectId": row["project_id"], "name": row["name"],
                "metadata": json.loads(row["metadata_json"]),
                "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def get(self, path: str, user, query_string: str = "") -> tuple[int, dict]:
        connection, workspace = self._context(user)
        workspace_id = workspace["id"]
        try:
            query = parse_qs(query_string)
            if path == "/api/v2/health":
                return 200, {"ok": True, "version": 2}
            if path == "/api/v2/bootstrap":
                media_count = connection.execute(
                    "SELECT COUNT(*) FROM media_assets_v2 WHERE workspace_id=?", (workspace_id,)
                ).fetchone()[0]
                reusable_count = connection.execute(
                    """SELECT COUNT(DISTINCT l.media_id) FROM asset_links_v2 l
                       JOIN media_assets_v2 m ON m.id=l.media_id
                       WHERE m.workspace_id=? AND l.target_type='workspace' AND l.role='reusable_asset'""",
                    (workspace_id,),
                ).fetchone()[0]
                return 200, {
                    "workspace": {"id": workspace_id, "name": workspace["name"]},
                    "projects": self._projects(connection, workspace_id),
                    "counts": {"media": media_count, "reusable": reusable_count},
                    "featureFlags": {
                        "legacyPreserved": True,
                        "classificationDrafts": True,
                        "publicDiscovery": True,
                    },
                }
            if path == "/api/v2/projects":
                return 200, {"projects": self._projects(connection, workspace_id)}
            if path == "/api/v2/overview":
                return 200, overview(connection, workspace_id)
            if path == "/api/v2/projects/trash":
                rows = connection.execute("SELECT id,name,description,aspect,updated_at FROM projects_v2 WHERE workspace_id=? AND owner_user_id=? AND status='deleted' ORDER BY updated_at DESC", (workspace_id, user["id"])).fetchall()
                return 200, {"projects": [{"id": r["id"], "name": r["name"], "description": r["description"], "aspect": r["aspect"], "updatedAt": r["updated_at"]} for r in rows]}
            if path.startswith("/api/v2/projects/"):
                project_id = path.rsplit("/", 1)[-1]
                return 200, project_detail(connection, workspace_id, project_id)
            if path == "/api/v2/entities":
                return 200, {"entities": self._entities(
                    connection, workspace_id,
                    (query.get("type") or [""])[0],
                    (query.get("projectId") or [""])[0],
                )}
            if path == "/api/v2/media":
                try:
                    limit = min(max(int((query.get("limit") or ["60"])[0]), 1), 200)
                    offset = max(int((query.get("offset") or ["0"])[0]), 0)
                except ValueError as exc:
                    raise ValueError("分页参数不正确") from exc
                return 200, list_media(
                    connection, workspace_id,
                    query=(query.get("q") or [""])[0],
                    kind=(query.get("kind") or [""])[0],
                    source=(query.get("source") or [""])[0],
                    project_id=(query.get("projectId") or [""])[0],
                    lifecycle=(query.get("state") or ["active"])[0],
                    limit=limit, offset=offset,
                )
            if path.startswith("/api/v2/media/"):
                media_id = path.rsplit("/", 1)[-1]
                row = require_owned_media(connection, media_id, user["id"], active_only=False)
                links = connection.execute(
                    """SELECT id,target_type,target_id,role,classification_revision_id,created_at
                       FROM asset_links_v2 WHERE media_id=? ORDER BY created_at DESC""",
                    (media_id,),
                ).fetchall()
                return 200, {
                    "media": serialize_media(row, [
                        {
                            "id": item["id"], "targetType": item["target_type"],
                            "targetId": item["target_id"], "role": item["role"],
                            "classificationRevisionId": item["classification_revision_id"],
                        }
                        for item in links
                    ]),
                    "classifications": list_revisions(connection, user, media_id),
                }
            if path == "/api/v2/tasks":
                project_id = (query.get("projectId") or [""])[0]
                clauses = ["workspace_id=?"]
                params = [workspace_id]
                if project_id:
                    clauses.append("project_id=?");params.append(project_id)
                elif (query.get("standalone") or [""])[0] == "1":
                    clauses.append("project_id IS NULL")
                rows = connection.execute(
                    f"""SELECT id,project_id,shot_id,kind,status,title,requested_count,
                               submitted_job_ids_json,error,created_at,updated_at,
                               (SELECT COUNT(*) FROM generation_versions_v2 v
                                WHERE json_extract(v.parameters_json,'$.taskId')=generation_tasks_v2.id) AS completed_count
                        FROM generation_tasks_v2 WHERE {' AND '.join(clauses)}
                        ORDER BY updated_at DESC LIMIT 200""", params,
                ).fetchall()
                return 200, {"tasks": [{
                    "id": row["id"], "projectId": row["project_id"], "shotId": row["shot_id"],
                    "kind": row["kind"], "status": row["status"], "title": row["title"],
                    "count": row["requested_count"], "jobIds": json.loads(row["submitted_job_ids_json"]),
                    "completedCount": row["completed_count"],
                    "error": row["error"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                } for row in rows]}
            if path == "/api/v2/deliveries":
                project_id = (query.get("projectId") or [""])[0]
                require_project(connection, workspace_id, project_id)
                rows = connection.execute(
                    """SELECT d.*,m.display_name,m.media_kind,m.storage_scope,m.storage_path
                       FROM deliveries_v2 d JOIN media_assets_v2 m ON m.id=d.media_id
                       WHERE d.project_id=? ORDER BY d.updated_at DESC""", (project_id,),
                ).fetchall()
                return 200, {"deliveries": [{
                    "id": row["id"], "mediaId": row["media_id"], "name": row["name"],
                    "kind": row["media_kind"], "status": row["status"],
                    "metadata": json.loads(row["metadata_json"]), "updatedAt": row["updated_at"],
                } for row in rows]}
            if path.startswith("/api/v2/tasks/"):
                return 200, task_detail(connection, workspace_id, path.rsplit("/", 1)[-1])
            if path == "/api/v2/classifications":
                media_id = (query.get("mediaId") or [""])[0]
                require_owned_media(connection, media_id, user["id"])
                return 200, {"revisions": list_revisions(connection, user, media_id)}
            return 404, {"error": "v2_route_not_found"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        finally:
            connection.close()

    def post(self, path: str, user, body: dict, *, input_root: Path,
             output_root: Path) -> tuple[int, dict]:
        connection, workspace = self._context(user)
        workspace_id = workspace["id"]
        try:
            if path == "/api/v2/catalog/sync":
                return 200, sync_legacy_catalog(
                    connection, user, workspace_id, Path(input_root), Path(output_root)
                )
            if path == "/api/v2/projects":
                name = str(body.get("name", "")).strip()
                if not name:
                    raise ValueError("项目名称不能为空")
                now = int(time.time())
                project_id = new_id("project")
                with connection:
                    connection.execute(
                        """INSERT INTO projects_v2(
                            id,workspace_id,owner_user_id,name,description,status,aspect,created_at,updated_at
                        ) VALUES(?,?,?,?,?,'active',?,?,?)""",
                        (project_id, workspace_id, user["id"], name,
                         str(body.get("description", "")), str(body.get("aspect", "16:9")), now, now),
                    )
                return 201, {"project": {"id": project_id, "name": name, "status": "active"}}
            if path in {"/api/v2/projects/delete", "/api/v2/projects/restore"}:
                project_id = str(body.get("projectId", ""))
                restoring = path.endswith('/restore')
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    project = connection.execute("SELECT * FROM projects_v2 WHERE id=? AND workspace_id=? AND owner_user_id=?", (project_id, workspace_id, user["id"])).fetchone()
                    if not project:
                        raise ValueError("项目不存在或无权操作")
                    if not restoring and connection.execute("SELECT 1 FROM generation_tasks_v2 WHERE project_id=? AND status IN ('queued','running') LIMIT 1", (project_id,)).fetchone():
                        raise ValueError("项目仍有生成任务，请等待完成或先取消任务再删除")
                    if restoring and project['status'] != 'deleted':
                        raise ValueError("项目不在回收站")
                    now = int(time.time())
                    connection.execute("UPDATE projects_v2 SET status=?,archived_at=?,updated_at=? WHERE id=?", ('active' if restoring else 'deleted', None if restoring else now, now, project_id))
                return 200, {"projectId": project_id, "status": 'active' if restoring else 'deleted', "mediaDeleted": False}
            if path == "/api/v2/projects/source":
                return 200, save_source(
                    connection, user, workspace_id, str(body.get("projectId", "")), body
                )
            if path == "/api/v2/projects/apply-analysis":
                analysis = body.get("analysis")
                if not isinstance(analysis, dict):
                    raise ValueError("分析结果必须是对象")
                return 200, apply_analysis(
                    connection, user, workspace_id, str(body.get("projectId", "")), analysis
                )
            if path == "/api/v2/projects/archive":
                project_id = str(body.get("projectId", ""));require_project(connection, workspace_id, project_id)
                now = int(time.time())
                with connection:
                    connection.execute(
                        "UPDATE projects_v2 SET status='archived',archived_at=?,updated_at=? WHERE id=?",
                        (now, now, project_id),
                    )
                return 200, {"projectId": project_id, "status": "archived"}
            if path == "/api/v2/entities":
                return self._create_entity(connection, workspace_id, user, body)
            if path == "/api/v2/entities/update":
                return 200, update_entity(
                    connection, user, workspace_id, str(body.get("entityId", "")), body
                )
            if path == "/api/v2/entities/link-project":
                entity_id = str(body.get("entityId", ""));project_id = str(body.get("projectId", ""))
                entity = connection.execute(
                    "SELECT id FROM entities_v2 WHERE id=? AND workspace_id=?", (entity_id, workspace_id)
                ).fetchone();require_project(connection, workspace_id, project_id)
                if not entity:raise ValueError("对象不存在")
                with connection:
                    connection.execute(
                        """INSERT OR IGNORE INTO entity_links_v2(
                            id,source_entity_id,target_type,target_id,role,created_by,created_at
                        ) VALUES(?,?,'project',?,'project_use',?,?)""",
                        (new_id("elink"), entity_id, project_id, user["id"], int(time.time())),
                    )
                return 200, {"linked": True, "entityId": entity_id, "projectId": project_id}
            if path == "/api/v2/tasks/create":
                project_id = body.get("projectId") or None
                if project_id:require_project(connection, workspace_id, str(project_id))
                return 201, create_task(
                    connection, user, workspace_id,
                    kind=str(body.get("kind", "video")), title=str(body.get("title", "生成任务")),
                    prompt=str(body.get("prompt", "")), parameters=body.get("parameters") or {},
                    input_media=body.get("inputMedia") or [], count=int(body.get("count") or 1),
                    project_id=str(project_id) if project_id else None,
                    shot_id=str(body.get("shotId")) if body.get("shotId") else None,
                    provider=str(body.get("provider", "")), model=str(body.get("model", "")),
                )
            if path == "/api/v2/tasks/mark":
                return 200, mark_task(
                    connection, str(body.get("taskId", "")), user["id"], str(body.get("status", "")),
                    job_ids=body.get("jobIds"), error=str(body.get("error", "")),
                    parameters_patch=body.get("parametersPatch") if isinstance(body.get("parametersPatch"), dict) else None,
                )
            if path == "/api/v2/versions/register":
                return 201, register_version(
                    connection, user, workspace_id, task_id=str(body.get("taskId", "")),
                    media_id=str(body.get("mediaId", "")), parent_version_id=body.get("parentVersionId") or None,
                )
            if path == "/api/v2/versions/state":
                return 200, set_version_state(
                    connection, user, workspace_id, str(body.get("shotId", "")),
                    str(body.get("versionId", "")), str(body.get("state", "candidate")),
                    str(body.get("reviewNote", "")),
                )
            if path == "/api/v2/deliveries/create":
                project_id=str(body.get("projectId", ""));media_id=str(body.get("mediaId", ""));require_project(connection,workspace_id,project_id);media=require_owned_media(connection,media_id,user["id"])
                now=int(time.time());delivery_id=new_id("delivery")
                with connection:
                    connection.execute(
                        """INSERT INTO deliveries_v2(
                            id,project_id,media_id,name,status,metadata_json,created_by,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (delivery_id,project_id,media_id,str(body.get("name") or media["display_name"])[:120],str(body.get("status") or "draft"),json.dumps(body.get("metadata") or {},ensure_ascii=False),user["id"],now,now),
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO asset_links_v2(
                            id,media_id,target_type,target_id,role,created_by,created_at
                        ) VALUES(?,?,'project',?,'final_clip',?,?)""",
                        (new_id("link"),media_id,project_id,user["id"],now),
                    )
                return 201,{"delivery":{"id":delivery_id,"mediaId":media_id,"status":str(body.get("status") or "draft")}}
            if path == "/api/v2/deliveries/status":
                delivery_id=str(body.get("deliveryId", ""));status=str(body.get("status", ""))
                if status not in {"draft","review","approved","delivered"}:raise ValueError("交付状态不正确")
                row=connection.execute("SELECT d.id FROM deliveries_v2 d JOIN projects_v2 p ON p.id=d.project_id WHERE d.id=? AND p.workspace_id=?",(delivery_id,workspace_id)).fetchone()
                if not row:raise ValueError("交付项不存在")
                with connection:connection.execute("UPDATE deliveries_v2 SET status=?,updated_at=? WHERE id=?",(status,int(time.time()),delivery_id))
                return 200,{"deliveryId":delivery_id,"status":status}
            if path == "/api/v2/shares/publish":
                return 201, publish(
                    connection, user, workspace_id, str(body.get("mediaId", "")),
                    str(body.get("title", "")).strip(), body.get("recipe"), body.get("permissions"),
                )
            if path == "/api/v2/shares/withdraw":
                return 200, withdraw(connection, user, str(body.get("shareId", "")))
            if path == "/api/v2/remix-drafts":
                return 201, remix_draft(connection, str(body.get("shareId", "")))
            if path == "/api/v2/shares/favorite":
                return 200, toggle_favorite(connection, user, str(body.get("shareId", "")))
            if path == "/api/v2/shares/report":
                return 201, report_share(
                    connection, user, str(body.get("shareId", "")),
                    str(body.get("reason", "other")), str(body.get("detail", "")),
                )
            if path == "/api/v2/media/promote":
                return 200, promote_without_copy(
                    connection, str(body.get("mediaId", "")), workspace_id, user["id"]
                )
            if path == "/api/v2/media/tags":
                return 200, update_tags(connection, str(body.get("mediaId", "")), user["id"], body.get("tags"))
            if path == "/api/v2/media/rename":
                return 200, update_display_name(
                    connection, str(body.get("mediaId", "")), user["id"], str(body.get("displayName", ""))
                )
            if path == "/api/v2/media/unlink":
                return 200, unlink_media(
                    connection, str(body.get("mediaId", "")), user["id"], str(body.get("linkId", ""))
                )
            if path == "/api/v2/media/trash":
                return 200, set_lifecycle(connection, str(body.get("mediaId", "")), user["id"], "trash")
            if path == "/api/v2/media/restore":
                return 200, set_lifecycle(connection, str(body.get("mediaId", "")), user["id"], "active")
            if path == "/api/v2/media/hash":
                return 200, compute_hash(
                    connection, str(body.get("mediaId", "")), user["id"], Path(input_root), Path(output_root)
                )
            if path == "/api/v2/media/permanent-delete":
                return 200, permanent_delete(
                    connection, str(body.get("mediaId", "")), user["id"], Path(input_root), Path(output_root)
                )
            if path == "/api/v2/media/bulk":
                media_ids = body.get("mediaIds")
                if not isinstance(media_ids, list) or not media_ids or len(media_ids) > 200:
                    raise ValueError("请选择 1 到 200 项素材")
                if any(not isinstance(media_id, str) or not media_id for media_id in media_ids):
                    raise ValueError("素材 ID 不正确")
                media_ids = list(dict.fromkeys(media_ids))
                action = str(body.get("action", ""))
                if action not in {"trash", "restore", "link_project", "permanent_delete"}:
                    raise ValueError("批量操作不正确")
                # Validate ownership before reversible batch actions so they do
                # not partially succeed when one ID is invalid.
                if action != "permanent_delete":
                    for media_id in media_ids:
                        require_owned_media(connection, media_id, user["id"], active_only=False)
                results = []
                failures = []
                if action == "link_project":
                    project_id = str(body.get("projectId", ""))
                    self._validate_target(connection, workspace_id, "project", project_id)
                    for media_id in media_ids:
                        row = require_owned_media(connection, media_id, user["id"])
                        result = link_media(connection, row["id"], "project", project_id, "reference", user["id"])
                        results.append({"mediaId": media_id, **result})
                elif action in {"trash", "restore"}:
                    target_state = "trash" if action == "trash" else "active"
                    for media_id in media_ids:
                        results.append(set_lifecycle(connection, media_id, user["id"], target_state))
                else:
                    # Permanent file removal can be partially successful. Each
                    # protected item remains in trash with its precise reason.
                    for media_id in media_ids:
                        try:
                            results.append(permanent_delete(
                                connection, media_id, user["id"], Path(input_root), Path(output_root)
                            ))
                        except ValueError as exc:
                            failures.append({"mediaId": media_id, "error": str(exc)})
                return 200, {
                    "action": action, "requested": len(media_ids),
                    "succeeded": len(results), "failed": len(failures),
                    "results": results, "failures": failures,
                }
            if path == "/api/v2/media/link":
                media_id = str(body.get("mediaId", ""))
                target_type = str(body.get("targetType", ""))
                target_id = str(body.get("targetId", ""))
                self._validate_target(connection, workspace_id, target_type, target_id)
                return 201, link_media(
                    connection, media_id, target_type, target_id,
                    str(body.get("role", "reference")), user["id"],
                )
            if path == "/api/v2/classifications/draft":
                data = body.get("classification") or {}
                if not isinstance(data, dict):
                    raise ValueError("分类结果必须是对象")
                return 201, create_draft(
                    connection, user, workspace_id, str(body.get("mediaId", "")), data,
                    str(body.get("provider", "manual")), str(body.get("model", "")),
                )
            if path == "/api/v2/classifications/edit":
                data = body.get("classification")
                if not isinstance(data, dict):
                    raise ValueError("分类结果必须是对象")
                return 200, update_draft(connection, user, str(body.get("revisionId", "")), data)
            if path == "/api/v2/classifications/confirm":
                data = body.get("classification")
                if data is not None and not isinstance(data, dict):
                    raise ValueError("分类结果必须是对象")
                return 200, confirm_archive(
                    connection, user, workspace_id, str(body.get("revisionId", "")), data
                )
            return 404, {"error": "v2_route_not_found"}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except sqlite3.IntegrityError as exc:
            return 409, {"error": "数据冲突", "detail": str(exc)}
        finally:
            connection.close()

    def _create_entity(self, connection, workspace_id: str, user, body: dict) -> tuple[int, dict]:
        entity_type = str(body.get("type", ""))
        name = str(body.get("name", "")).strip()
        if entity_type not in ENTITY_TYPES:
            raise ValueError("实体类型不正确")
        if not name:
            raise ValueError("名称不能为空")
        parent_id = body.get("parentId") or None
        project_id = body.get("projectId") or None
        if parent_id and not connection.execute(
            "SELECT id FROM entities_v2 WHERE id=? AND workspace_id=?", (parent_id, workspace_id)
        ).fetchone():
            raise ValueError("上级对象不存在")
        if project_id and not connection.execute(
            "SELECT id FROM projects_v2 WHERE id=? AND workspace_id=?", (project_id, workspace_id)
        ).fetchone():
            raise ValueError("项目不存在")
        now = int(time.time())
        entity_id = new_id(entity_type)
        with connection:
            connection.execute(
                """INSERT INTO entities_v2(
                    id,workspace_id,owner_user_id,entity_type,parent_id,project_id,name,
                    metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (entity_id, workspace_id, user["id"], entity_type, parent_id, project_id, name,
                 json.dumps(body.get("metadata") or {}, ensure_ascii=False), now, now),
            )
        return 201, {"entity": {"id": entity_id, "type": entity_type, "name": name}}

    @staticmethod
    def _validate_target(connection, workspace_id: str, target_type: str, target_id: str) -> None:
        if target_type == "workspace":
            row = target_id == workspace_id
        elif target_type == "project":
            row = connection.execute(
                "SELECT id FROM projects_v2 WHERE id=? AND workspace_id=?", (target_id, workspace_id)
            ).fetchone()
        elif target_type == "media":
            row = connection.execute(
                "SELECT id FROM media_assets_v2 WHERE id=? AND workspace_id=?", (target_id, workspace_id)
            ).fetchone()
        elif target_type in ENTITY_TYPES:
            row = connection.execute(
                "SELECT id FROM entities_v2 WHERE id=? AND workspace_id=? AND entity_type=?",
                (target_id, workspace_id, target_type),
            ).fetchone()
        else:
            row = None
        if not row:
            raise ValueError("关联对象不存在")
