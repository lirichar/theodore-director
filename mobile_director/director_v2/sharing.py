"""Public discovery contracts with explicit publishing and safe remix drafts."""

from __future__ import annotations

import json
import math
import re
import time

from .database import new_id


DEFAULT_PERMISSIONS = {
    "platformUse": True,
    "remix": True,
    "attributionRequired": True,
    "originalDownload": False,
}

RECIPE_FIELDS = {
    "generator", "prompt", "negativePrompt", "aspect", "duration",
    "camera", "style", "parameterValues", "inputRoles",
}

PARAMETER_FIELDS = {
    "mode", "aspect", "resolution", "duration", "seed", "size", "negative",
}


def _safe_text(value, limit=12000):
    if not isinstance(value, str):
        raise ValueError("公开配方文本格式不正确")
    if re.search(r"(?i)(api[_-]?key|authorization|bearer\s|workflow_json|[a-z]:[\\/]|/(?:home|users|mnt|etc)/|sk-[a-z0-9_-]{16,})", value):
        raise ValueError("制作配方包含不可公开的字段或本地路径")
    return value[:limit]


def public_descriptor(classification) -> dict:
    """Only the descriptor explicitly pinned by the publisher is public."""
    raw = classification.get("retrievalDescriptor", {}) if isinstance(classification, dict) else {}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key in ("displayLabel", "searchCaption", "usageNotes"):
        if isinstance(raw.get(key), str):
            result[key] = raw[key][:4000]
    for key in ("bestFor", "avoidFor", "keywords"):
        if isinstance(raw.get(key), list):
            result[key] = [item[:500] for item in raw[key][:100] if isinstance(item, str)]
    return result


def sanitize_recipe(recipe) -> dict:
    if not isinstance(recipe, dict):
        return {}
    clean = {}
    for key in RECIPE_FIELDS - {"parameterValues", "inputRoles", "duration"}:
        if key in recipe:
            clean[key] = _safe_text(recipe[key])
    if "duration" in recipe:
        value = recipe["duration"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 60:
            raise ValueError("公开配方时长不正确")
        clean["duration"] = value
    if "parameterValues" in recipe:
        raw = recipe["parameterValues"]
        if not isinstance(raw, dict):
            raise ValueError("公开配方参数必须是对象")
        parameters = {}
        for key in PARAMETER_FIELDS & raw.keys():
            value = raw[key]
            if isinstance(value, str):
                parameters[key] = _safe_text(value, 4000)
            elif not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value):
                parameters[key] = value
            else:
                raise ValueError("公开配方参数只允许文本或有限数值")
        clean["parameterValues"] = parameters
    if "inputRoles" in recipe:
        roles = recipe["inputRoles"]
        if not isinstance(roles, list) or any(role not in {"reference", "first_frame", "last_frame", "identity_reference", "environment_reference"} for role in roles if isinstance(role, str)) or any(not isinstance(role, str) for role in roles):
            raise ValueError("公开配方参考角色不正确")
        clean["inputRoles"] = roles[:12]
    return clean


def publish(connection, user, workspace_id: str, media_id: str, title: str,
            recipe=None, permissions=None) -> dict:
    media = connection.execute(
        "SELECT * FROM media_assets_v2 WHERE id=? AND workspace_id=? AND owner_user_id=? AND lifecycle_state='active'",
        (media_id, workspace_id, user["id"]),
    ).fetchone()
    if not media:
        raise ValueError("媒体不存在或无权发布")
    revision = connection.execute(
        """SELECT id FROM classification_revisions_v2
           WHERE media_id=? AND created_by=? AND state='archived'
           ORDER BY archived_at DESC, rowid DESC LIMIT 1""",
        (media_id, user["id"]),
    ).fetchone()
    if not revision:
        raise ValueError("请先确认并归档素材分类")
    permission_data = dict(DEFAULT_PERMISSIONS)
    if isinstance(permissions, dict):
        for key in DEFAULT_PERMISSIONS:
            if key in permissions:
                if not isinstance(permissions[key], bool):
                    raise ValueError("分享权限必须是布尔值")
                permission_data[key] = permissions[key]
    # Product policy: public original download stays off in this release.
    permission_data["originalDownload"] = False
    clean_recipe = sanitize_recipe(recipe)
    now = int(time.time())
    existing = connection.execute(
        "SELECT id FROM shares_v2 WHERE media_id=? AND owner_user_id=? AND visibility='public'",
        (media_id, user["id"]),
    ).fetchone()
    if existing:
        with connection:
            connection.execute(
                """UPDATE shares_v2 SET title=?,classification_revision_id=?,permissions_json=?,
                   recipe_json=?,updated_at=? WHERE id=?""",
                (title[:100] or media["display_name"], revision["id"],
                 json.dumps(permission_data), json.dumps(clean_recipe, ensure_ascii=False), now, existing["id"]),
            )
        share_id = existing["id"]
    else:
        share_id = new_id("share")
        with connection:
            connection.execute(
                """INSERT INTO shares_v2(
                    id,media_id,owner_user_id,visibility,title,classification_revision_id,
                    permissions_json,recipe_json,created_at,updated_at,withdrawn_at
                ) VALUES(?,?,?,'public',?,?,?,?,?,?,NULL)""",
                (share_id, media_id, user["id"], title[:100] or media["display_name"], revision["id"],
                 json.dumps(permission_data), json.dumps(clean_recipe, ensure_ascii=False), now, now),
            )
    return {"shareId": share_id, "visibility": "public", "permissions": permission_data}


def withdraw(connection, user, share_id: str) -> dict:
    now = int(time.time())
    with connection:
        cursor = connection.execute(
            """UPDATE shares_v2 SET visibility='withdrawn',withdrawn_at=?,updated_at=?
               WHERE id=? AND owner_user_id=? AND visibility='public'""",
            (now, now, share_id, user["id"]),
        )
    if not cursor.rowcount:
        raise ValueError("分享不存在或已撤回")
    return {"shareId": share_id, "visibility": "withdrawn"}


def public_listing(connection, query: str = "", limit: int = 48, offset: int = 0) -> dict:
    clauses = ["s.visibility='public'", "m.lifecycle_state='active'", "r.state='archived'"]
    params = []
    if query:
        clauses.append("(s.title LIKE ? OR json_extract(r.data_json,'$.retrievalDescriptor.displayLabel','$.retrievalDescriptor.searchCaption','$.retrievalDescriptor.bestFor','$.retrievalDescriptor.avoidFor','$.retrievalDescriptor.usageNotes','$.retrievalDescriptor.keywords') LIKE ?)")
        term = f"%{query}%"
        params.extend([term, term])
    where = " AND ".join(clauses)
    total = connection.execute(
        f"""SELECT COUNT(*) FROM shares_v2 s JOIN media_assets_v2 m ON m.id=s.media_id
            JOIN classification_revisions_v2 r ON r.id=s.classification_revision_id
            WHERE {where}""", params,
    ).fetchone()[0]
    rows = connection.execute(
        f"""SELECT s.id,s.title,s.permissions_json,s.updated_at,m.media_kind,m.retrieval_json,
                   u.username,r.data_json,(SELECT COUNT(*) FROM favorites_v2 f WHERE f.share_id=s.id) AS favorite_count
            FROM shares_v2 s JOIN media_assets_v2 m ON m.id=s.media_id
            JOIN users u ON u.id=s.owner_user_id
            JOIN classification_revisions_v2 r ON r.id=s.classification_revision_id
            WHERE {where} ORDER BY s.updated_at DESC LIMIT ? OFFSET ?""",
        (*params, min(max(limit, 1), 100), max(offset, 0)),
    ).fetchall()
    items = []
    for row in rows:
        classification = json.loads(row["data_json"])
        items.append({
            "id": row["id"], "title": row["title"], "kind": row["media_kind"],
            "assetType": classification.get("assetType", "other"),
            "retrieval": public_descriptor(classification),
            "creator": row["username"], "permissions": json.loads(row["permissions_json"]),
            "preview": f"/api/v2/public-preview/{row['id']}", "favoriteCount": row["favorite_count"],
            "updatedAt": row["updated_at"],
        })
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "hasMore": offset + len(items) < total}


def remix_draft(connection, share_id: str) -> dict:
    row = connection.execute(
        """SELECT s.id,s.title,s.recipe_json,s.permissions_json,m.media_kind,r.data_json,u.username
           FROM shares_v2 s JOIN media_assets_v2 m ON m.id=s.media_id
           JOIN users u ON u.id=s.owner_user_id
           JOIN classification_revisions_v2 r ON r.id=s.classification_revision_id
           WHERE s.id=? AND s.visibility='public' AND m.lifecycle_state='active' AND r.state='archived'""",
        (share_id,),
    ).fetchone()
    if not row:
        raise ValueError("公开素材不存在或已撤回")
    permissions = json.loads(row["permissions_json"])
    if not permissions.get("remix") or not permissions.get("platformUse"):
        raise ValueError("作者未开放制作同款")
    return {
        "draft": {
            "sourceShareId": row["id"], "sourcePreview": f"/api/v2/public-preview/{row['id']}",
            "title": f"制作同款 · {row['title']}", "recipe": sanitize_recipe(json.loads(row["recipe_json"] or "{}")),
            "retrieval": public_descriptor(json.loads(row["data_json"])),
            "attribution": row["username"] if permissions.get("attributionRequired") else "",
            "status": "editable", "submitted": False,
        }
    }


def public_media_row(connection, share_id: str):
    return connection.execute(
        """SELECT m.* FROM shares_v2 s JOIN media_assets_v2 m ON m.id=s.media_id
           WHERE s.id=? AND s.visibility='public' AND m.lifecycle_state='active'""",
        (share_id,),
    ).fetchone()


def toggle_favorite(connection, user, share_id: str) -> dict:
    share = connection.execute(
        "SELECT id FROM shares_v2 WHERE id=? AND visibility='public'", (share_id,)
    ).fetchone()
    if not share:
        raise ValueError("公开素材不存在")
    existing = connection.execute(
        "SELECT 1 FROM favorites_v2 WHERE user_id=? AND share_id=?", (user["id"], share_id)
    ).fetchone()
    with connection:
        if existing:
            connection.execute("DELETE FROM favorites_v2 WHERE user_id=? AND share_id=?", (user["id"], share_id))
        else:
            connection.execute(
                "INSERT INTO favorites_v2(user_id,share_id,created_at) VALUES(?,?,?)",
                (user["id"], share_id, int(time.time())),
            )
    return {"shareId": share_id, "favorited": not bool(existing)}


def report_share(connection, user, share_id: str, reason: str, detail: str = "") -> dict:
    if reason not in {"copyright", "privacy", "unsafe", "misleading", "other"}:
        raise ValueError("请选择有效的举报原因")
    share = connection.execute("SELECT id FROM shares_v2 WHERE id=?", (share_id,)).fetchone()
    if not share:
        raise ValueError("素材不存在")
    report_id = new_id("report")
    with connection:
        connection.execute(
            """INSERT INTO share_reports_v2(
                id,share_id,reporter_user_id,reason,detail,status,created_at
            ) VALUES(?,?,?,?,?,'open',?) ON CONFLICT(share_id,reporter_user_id) DO UPDATE SET
              reason=excluded.reason,detail=excluded.detail,status='open',created_at=excluded.created_at""",
            (report_id, share_id, user["id"], reason, detail[:1000], int(time.time())),
        )
    return {"shareId": share_id, "reported": True}
