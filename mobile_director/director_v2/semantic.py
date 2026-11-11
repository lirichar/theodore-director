"""Provider-neutral embedding cache and semantic media ranking."""

from __future__ import annotations

import hashlib
import json
import math
import time


def descriptor_text(row) -> str:
    data = json.loads(row["retrieval_json"] or "{}")
    parts = [data.get("displayLabel", ""), data.get("searchCaption", ""), data.get("usageNotes", "")]
    parts.extend(data.get("bestFor") or [])
    parts.extend(data.get("avoidFor") or [])
    parts.extend(data.get("keywords") or [])
    return "\n".join(str(part).strip() for part in parts if str(part).strip())


def pending_inputs(connection, workspace_id: str, model: str, limit: int = 2000, provider: str | None = None) -> list[dict]:
    rows = connection.execute(
        """SELECT id,retrieval_json FROM media_assets_v2
           WHERE workspace_id=? AND lifecycle_state='active' AND retrieval_json<>'{}'
           ORDER BY updated_at DESC LIMIT ?""", (workspace_id, limit),
    ).fetchall()
    pending = []
    for row in rows:
        text = descriptor_text(row)
        if not text:
            continue
        signature = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cached = connection.execute(
            "SELECT source_revision_id,provider FROM media_embeddings_v2 WHERE media_id=? AND model=?",
            (row["id"], model),
        ).fetchone()
        if not cached or cached["source_revision_id"] != signature or (provider is not None and cached["provider"] != provider):
            pending.append({"mediaId": row["id"], "text": text, "signature": signature})
    return pending


def save_vectors(connection, provider: str, model: str, inputs: list[dict], vectors: list[list[float]]) -> None:
    if len(inputs) != len(vectors):
        raise ValueError("嵌入模型返回数量不一致")
    now = int(time.time())
    with connection:
        for item, vector in zip(inputs, vectors):
            if not valid_vector(vector):
                raise ValueError("嵌入模型必须返回非空有限数值向量")
            connection.execute(
                """INSERT INTO media_embeddings_v2(
                    media_id,provider,model,dimensions,vector_json,source_revision_id,created_at
                ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(media_id,model) DO UPDATE SET
                  provider=excluded.provider,dimensions=excluded.dimensions,
                  vector_json=excluded.vector_json,source_revision_id=excluded.source_revision_id,
                  created_at=excluded.created_at""",
                (item["mediaId"], provider, model, len(vector), json.dumps(vector), item["signature"], now),
            )


def valid_vector(vector):
    return isinstance(vector, list) and bool(vector) and all(
        not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
        for value in vector
    )


def _cosine(left: list[float], right: list[float]) -> float:
    if not valid_vector(left) or not valid_vector(right) or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else -1.0


def search(connection, workspace_id: str, model: str, query_vector: list[float], limit: int = 60, provider: str | None = None) -> list[dict]:
    if not valid_vector(query_vector):
        raise ValueError("查询向量必须是非空有限数值向量")
    rows = connection.execute(
        """SELECT m.*,e.vector_json,e.source_revision_id FROM media_embeddings_v2 e
           JOIN media_assets_v2 m ON m.id=e.media_id
           WHERE m.workspace_id=? AND m.lifecycle_state='active' AND e.model=?
             AND (? IS NULL OR e.provider=?)""",
        (workspace_id, model, provider, provider),
    ).fetchall()
    rows = [row for row in rows if row["source_revision_id"] == hashlib.sha256(descriptor_text(row).encode("utf-8")).hexdigest()]
    ranked = sorted(
        ((row, _cosine(query_vector, json.loads(row["vector_json"]))) for row in rows),
        key=lambda item: item[1], reverse=True,
    )[:max(1, min(200, limit))]
    return [(row, score) for row, score in ranked if score > -1]
