from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import quote

from .database import new_id


MEDIA_EXTENSIONS = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".bmp": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
    ".mkv": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".flac": "audio",
    ".m4a": "audio",
    ".ogg": "audio",
}


def media_kind(path: Path) -> str | None:
    return MEDIA_EXTENSIONS.get(path.suffix.lower())


def deterministic_media_id(user_id: int, scope: str, relative_path: str) -> str:
    value = f"{user_id}:{scope}:{relative_path}".encode("utf-8")
    return "media_" + hashlib.sha256(value).hexdigest()[:32]


def media_url(row) -> str:
    return f"/api/media/{row['storage_scope']}/{quote(row['storage_path'], safe='/')}"


def preview_url(row) -> str | None:
    if row["media_kind"] == "image":
        return f"/api/thumb/{row['storage_scope']}/{quote(row['storage_path'], safe='/')}"
    if row["media_kind"] == "video":
        return f"/api/vthumb/{row['storage_scope']}/{quote(row['storage_path'], safe='/')}"
    return None


def serialize_media(row, links=None) -> dict:
    retrieval = json.loads(row["retrieval_json"] or "{}")
    return {
        "id": row["id"],
        "name": row["display_name"],
        "kind": row["media_kind"],
        "source": row["source_kind"],
        "state": row["lifecycle_state"],
        "size": row["byte_size"],
        "sha256": row["sha256"],
        "url": media_url(row),
        "preview": preview_url(row),
        "retrieval": retrieval,
        "tags": json.loads(row["tags_json"] or "[]") if "tags_json" in row.keys() else [],
        "links": links or [],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _iter_files(root: Path, recursive: bool, user_role: str):
    resolved_root = root.resolve()
    iterator = root.rglob("*") if recursive else root.iterdir()
    for path in iterator:
        if not path.is_file() or not media_kind(path):
            continue
        # ComfyUI input aliases refer to an existing canonical asset, not new media.
        if re.fullmatch(r"_ref_media_[0-9a-f]{32}", path.stem):
            continue
        if path.is_symlink() or resolved_root not in path.resolve().parents:
            continue
        if user_role == "admin" and recursive:
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] in {"TheodoreUsers", "TheodorePublic"}:
                continue
        yield path


def sync_legacy_catalog(connection, user, workspace_id: str, input_root: Path, output_root: Path) -> dict:
    now = int(time.time())
    created = 0
    updated = 0
    seen = 0
    seen_ids: set[str] = set()
    existing_ids = {
        row["id"] for row in connection.execute(
            "SELECT id FROM media_assets_v2 WHERE workspace_id=? AND owner_user_id=?",
            (workspace_id, user["id"]),
        ).fetchall()
    }
    for scope, root, recursive, source in (
        ("input", input_root, False, "legacy_input"),
        ("output", output_root, True, "legacy_result"),
    ):
        if not root.exists():
            continue
        for path in _iter_files(root, recursive, user["role"]):
            seen += 1
            relative = path.relative_to(root).as_posix()
            media_id = deterministic_media_id(user["id"], scope, relative)
            seen_ids.add(media_id)
            stats = path.stat()
            connection.execute(
                """INSERT INTO media_assets_v2(
                    id,workspace_id,owner_user_id,display_name,media_kind,
                    storage_scope,storage_path,byte_size,mime_type,source_kind,
                    lifecycle_state,retrieval_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(owner_user_id,storage_scope,storage_path) DO UPDATE SET
                    media_kind=excluded.media_kind,
                    sha256=CASE WHEN media_assets_v2.byte_size<>excluded.byte_size
                                THEN NULL ELSE media_assets_v2.sha256 END,
                    byte_size=excluded.byte_size,
                    mime_type=excluded.mime_type,
                    lifecycle_state=CASE WHEN media_assets_v2.lifecycle_state='missing'
                                         THEN 'active' ELSE media_assets_v2.lifecycle_state END,
                    updated_at=MAX(media_assets_v2.updated_at,excluded.updated_at)""",
                (
                    media_id,
                    workspace_id,
                    user["id"],
                    path.stem,
                    media_kind(path),
                    scope,
                    relative,
                    stats.st_size,
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    source,
                    "active",
                    "{}",
                    int(stats.st_mtime),
                    int(stats.st_mtime),
                ),
            )
            if media_id in existing_ids:
                updated += 1
            else:
                created += 1
    missing = 0
    local_rows = connection.execute(
        """SELECT id FROM media_assets_v2
           WHERE workspace_id=? AND owner_user_id=?
             AND storage_scope IN ('input','output')
             AND lifecycle_state='active'""",
        (workspace_id, user["id"]),
    ).fetchall()
    for row in local_rows:
        if row["id"] not in seen_ids:
            connection.execute(
                "UPDATE media_assets_v2 SET lifecycle_state='missing',updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            missing += 1
    connection.execute(
        """INSERT INTO catalog_state_v2(workspace_id,last_scan_at,last_scan_count)
           VALUES(?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET
           last_scan_at=excluded.last_scan_at,last_scan_count=excluded.last_scan_count""",
        (workspace_id, now, seen),
    )
    connection.commit()
    return {"seen": seen, "created": created, "updated": updated, "missing": missing, "scannedAt": now}


def list_media(connection, workspace_id: str, *, query: str = "", kind: str = "", source: str = "", project_id: str = "", lifecycle: str = "active", limit: int = 60, offset: int = 0) -> dict:
    lifecycle = lifecycle if lifecycle in {"active", "trash", "missing"} else "active"
    clauses = ["m.workspace_id=?", "m.lifecycle_state=?"]
    params: list = [workspace_id, lifecycle]
    if query:
        clauses.append("(m.display_name LIKE ? OR m.retrieval_json LIKE ? OR m.tags_json LIKE ?)")
        term = f"%{query}%"
        params.extend([term, term, term])
    if kind in {"image", "video", "audio", "document"}:
        clauses.append("m.media_kind=?")
        params.append(kind)
    if source:
        clauses.append("m.source_kind=?")
        params.append(source)
    if project_id:
        clauses.append(
            "EXISTS(SELECT 1 FROM asset_links_v2 l WHERE l.media_id=m.id AND l.target_type='project' AND l.target_id=?)"
        )
        params.append(project_id)
    where = " AND ".join(clauses)
    total = connection.execute(
        f"SELECT COUNT(*) FROM media_assets_v2 m WHERE {where}", params
    ).fetchone()[0]
    rows = connection.execute(
        f"""SELECT m.* FROM media_assets_v2 m WHERE {where}
            ORDER BY m.updated_at DESC,m.id DESC LIMIT ? OFFSET ?""",
        (*params, max(1, min(200, limit)), max(0, offset)),
    ).fetchall()
    media_ids = [row["id"] for row in rows]
    links_by_media = {media_id: [] for media_id in media_ids}
    if media_ids:
        marks = ",".join("?" for _ in media_ids)
        for link in connection.execute(
            f"SELECT * FROM asset_links_v2 WHERE media_id IN ({marks}) ORDER BY created_at", media_ids
        ).fetchall():
            links_by_media[link["media_id"]].append(
                {
                    "id": link["id"],
                    "targetType": link["target_type"],
                    "targetId": link["target_id"],
                    "role": link["role"],
                }
            )
    return {
        "items": [serialize_media(row, links_by_media[row["id"]]) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": offset + len(rows) < total,
    }


def require_owned_media(connection, media_id: str, user_id: int, active_only: bool = True):
    sql = "SELECT * FROM media_assets_v2 WHERE id=? AND owner_user_id=?"
    if active_only:
        sql += " AND lifecycle_state='active'"
    row = connection.execute(sql, (media_id, user_id)).fetchone()
    if not row:
        raise ValueError("媒体不存在或无权操作")
    return row


def link_media(connection, media_id: str, target_type: str, target_id: str, role: str, user_id: int) -> dict:
    require_owned_media(connection, media_id, user_id)
    allowed_targets = {"workspace", "project", "episode", "scene", "shot", "character", "character_look", "location", "scene_variant", "theme", "media"}
    if target_type not in allowed_targets:
        raise ValueError("不支持的关联类型")
    role = role.strip()[:64]
    if not role:
        raise ValueError("关联用途不能为空")
    existing = connection.execute(
        "SELECT * FROM asset_links_v2 WHERE media_id=? AND target_type=? AND target_id=? AND role=?",
        (media_id, target_type, target_id, role),
    ).fetchone()
    if existing:
        return {"id": existing["id"], "existing": True}
    link_id = new_id("link")
    connection.execute(
        "INSERT INTO asset_links_v2(id,media_id,target_type,target_id,role,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
        (link_id, media_id, target_type, target_id, role, user_id, int(time.time())),
    )
    connection.commit()
    return {"id": link_id, "existing": False}


def promote_without_copy(connection, media_id: str, workspace_id: str, user_id: int) -> dict:
    row = require_owned_media(connection, media_id, user_id)
    before = row["byte_size"]
    link = link_media(connection, media_id, "workspace", workspace_id, "reusable_asset", user_id)
    after = connection.execute(
        "SELECT byte_size FROM media_assets_v2 WHERE id=?", (media_id,)
    ).fetchone()[0]
    return {
        "ok": True,
        "mediaId": media_id,
        "linkId": link["id"],
        "existing": link["existing"],
        "copied": False,
        "bytesBefore": before,
        "bytesAfter": after,
    }


def update_tags(connection, media_id: str, user_id: int, tags) -> dict:
    require_owned_media(connection, media_id, user_id)
    if not isinstance(tags, list) or len(tags) > 50 or any(not isinstance(tag, str) or len(tag) > 60 for tag in tags):
        raise ValueError("标签须为文本数组，最多 50 个，每个不超过 60 字")
    cleaned = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
    connection.execute("UPDATE media_assets_v2 SET tags_json=?,updated_at=? WHERE id=?",
                       (json.dumps(cleaned, ensure_ascii=False), int(time.time()), media_id))
    connection.commit()
    return {"mediaId": media_id, "tags": cleaned}


def update_display_name(connection, media_id: str, user_id: int, display_name: str) -> dict:
    row = require_owned_media(connection, media_id, user_id)
    name = str(display_name).strip()[:160]
    if not name:
        raise ValueError("显示名称不能为空")
    connection.execute(
        "UPDATE media_assets_v2 SET display_name=?,updated_at=? WHERE id=?",
        (name, int(time.time()), media_id),
    )
    connection.commit()
    return {"mediaId": media_id, "displayName": name, "storagePathChanged": False,
            "storagePath": row["storage_path"]}


def unlink_media(connection, media_id: str, user_id: int, link_id: str) -> dict:
    require_owned_media(connection, media_id, user_id)
    link = connection.execute(
        "SELECT classification_revision_id FROM asset_links_v2 WHERE id=? AND media_id=?",
        (link_id, media_id),
    ).fetchone()
    if not link:
        raise ValueError("关联不存在")
    if link["classification_revision_id"] is not None:
        raise ValueError("归档分类关联不可直接删除，请创建新分类草稿并确认归档")
    cursor = connection.execute(
        "DELETE FROM asset_links_v2 WHERE id=? AND media_id=? AND classification_revision_id IS NULL",
        (link_id, media_id),
    )
    connection.commit()
    if not cursor.rowcount:
        raise ValueError("关联不存在")
    return {"mediaId": media_id, "linkId": link_id, "removed": True, "fileDeleted": False}


def set_lifecycle(connection, media_id: str, user_id: int, state: str) -> dict:
    if state not in {"active", "trash"}:
        raise ValueError("媒体状态不正确")
    row = connection.execute(
        "SELECT * FROM media_assets_v2 WHERE id=? AND owner_user_id=?", (media_id, user_id)
    ).fetchone()
    if not row:
        raise ValueError("媒体不存在或无权操作")
    now = int(time.time())
    connection.execute(
        "UPDATE media_assets_v2 SET lifecycle_state=?,deleted_at=?,updated_at=? WHERE id=?",
        (state, now if state == "trash" else None, now, media_id),
    )
    connection.commit()
    return {"mediaId": media_id, "state": state, "fileDeleted": False}


def compute_hash(connection, media_id: str, user_id: int, input_root: Path,
                 output_root: Path) -> dict:
    row = require_owned_media(connection, media_id, user_id)
    root = input_root if row["storage_scope"] == "input" else output_root
    path = (Path(root) / row["storage_path"]).resolve()
    if Path(root).resolve() not in path.parents or not path.is_file():
        raise ValueError("媒体文件不存在")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    connection.execute(
        "UPDATE media_assets_v2 SET sha256=?,byte_size=?,updated_at=? WHERE id=?",
        (value, path.stat().st_size, int(time.time()), media_id),
    )
    connection.commit()
    duplicates = connection.execute(
        """SELECT id,display_name FROM media_assets_v2
           WHERE sha256=? AND id<>? AND lifecycle_state='active'
             AND workspace_id=? AND owner_user_id=?""",
        (value, media_id, row["workspace_id"], user_id),
    ).fetchall()
    return {"mediaId": media_id, "sha256": value,
            "duplicates": [{"id": item["id"], "name": item["display_name"]} for item in duplicates]}


def permanent_delete(connection, media_id: str, user_id: int, input_root: Path,
                     output_root: Path) -> dict:
    staged_path = None
    path = None
    try:
        with connection:
            # Acquire the write lock before checking references so another request
            # cannot publish or register a delivery between validation and delete.
            connection.execute(
                "UPDATE media_assets_v2 SET id=id WHERE id=? AND owner_user_id=?",
                (media_id, user_id),
            )
            row = require_owned_media(connection, media_id, user_id, active_only=False)
            if row["lifecycle_state"] != "trash":
                raise ValueError("请先将媒体移入回收站，再确认永久删除")
            reference_queries = (
                ("SELECT 1 FROM asset_links_v2 WHERE media_id=? OR (target_type='media' AND target_id=?) LIMIT 1", (media_id, media_id)),
                ("SELECT 1 FROM generation_versions_v2 WHERE media_id=? OR instr(input_media_json,?)>0 LIMIT 1", (media_id, media_id)),
                ("SELECT 1 FROM generation_tasks_v2 WHERE instr(input_media_json,?)>0 LIMIT 1", (media_id,)),
                ("SELECT 1 FROM deliveries_v2 WHERE media_id=? LIMIT 1", (media_id,)),
                ("SELECT 1 FROM shares_v2 WHERE media_id=? LIMIT 1", (media_id,)),
                ("SELECT 1 FROM classification_revisions_v2 WHERE media_id=? AND state='archived' LIMIT 1", (media_id,)),
            )
            if any(connection.execute(sql, params).fetchone() for sql, params in reference_queries):
                raise ValueError("媒体仍有项目、版本、任务、交付、分享或归档分类引用，可保留在回收站")
            if row["storage_scope"] not in {"input", "output"}:
                raise ValueError("暂不支持永久删除此存储位置的媒体")
            root = Path(input_root) if row["storage_scope"] == "input" else Path(output_root)
            unresolved = root / row["storage_path"]
            path = unresolved.resolve()
            if root.resolve() not in path.parents or unresolved.is_symlink():
                raise ValueError("媒体路径不安全")
            # Validate all SQL constraints first. A file is only renamed (same
            # volume, no duplicate bytes) until the database commit has succeeded.
            connection.execute("DELETE FROM classification_revisions_v2 WHERE media_id=?", (media_id,))
            connection.execute("DELETE FROM classification_jobs_v2 WHERE media_id=?", (media_id,))
            connection.execute("DELETE FROM media_assets_v2 WHERE id=?", (media_id,))
            if path.exists():
                if not path.is_file():
                    raise ValueError("媒体路径不是文件")
                staged_path = path.with_name(f".{new_id('theodore_delete')}.pending")
                path.rename(staged_path)
    except Exception:
        if staged_path is not None and staged_path.exists() and path is not None:
            staged_path.rename(path)
        raise
    cleanup_pending = False
    if staged_path is not None:
        try:
            staged_path.unlink()
        except OSError:
            # A file lock must not turn a successful database commit into a
            # misleading 500. Retained .pending bytes are intentionally hidden.
            cleanup_pending = True
    return {"mediaId": media_id, "permanentlyDeleted": not cleanup_pending,
            "recoverable": cleanup_pending, "cleanupPending": cleanup_pending}
