from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from director_v2.api import DirectorV2API


class DirectorV2CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "auth.db"
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """CREATE TABLE users(
                id INTEGER PRIMARY KEY, username TEXT UNIQUE, password BLOB, salt BLOB,
                role TEXT NOT NULL, created_at INTEGER NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO users VALUES(1,'tester',X'00',X'00','user',1)"
        )
        connection.commit()
        connection.close()
        self.input_root = self.root / "input"
        self.output_root = self.root / "output"
        self.input_root.mkdir()
        self.output_root.mkdir()
        (self.input_root / "林默-正面.png").write_bytes(b"not-a-real-png-but-indexable")
        (self.output_root / "shot_001.mp4").write_bytes(b"not-a-real-video-but-indexable")
        self.user = {"id": 1, "username": "tester", "role": "user"}
        self.api = DirectorV2API(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, body=None):
        return self.api.post(
            path, self.user, body or {},
            input_root=self.input_root, output_root=self.output_root,
        )

    def test_standalone_tags_survive_scan_and_are_searchable(self):
        self.post('/api/v2/catalog/sync')
        _, listing = self.api.get('/api/v2/media', self.user)
        item = listing['items'][0]
        status, saved = self.post('/api/v2/media/tags', {'mediaId': item['id'], 'tags': [' 雨夜 ', '雨夜', '蓝色', '']})
        self.assertEqual(status, 200)
        self.assertEqual(saved['tags'], ['雨夜', '蓝色'])
        self.post('/api/v2/catalog/sync')
        _, result = self.api.get('/api/v2/media', self.user, 'q=雨夜')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['tags'], ['雨夜', '蓝色'])
        self.assertEqual(result['items'][0]['links'], [])
        self.assertEqual(result['items'][0]['url'], item['url'])
        self.assertEqual(self.post('/api/v2/media/tags', {'mediaId': item['id'], 'tags': []})[0], 200)
        self.assertEqual(self.api.get('/api/v2/media', self.user, 'q=雨夜')[1]['total'], 0)

    def test_tags_validate_input_and_enforce_ownership(self):
        self.post('/api/v2/catalog/sync')
        _, listing = self.api.get('/api/v2/media', self.user)
        media_id = listing['items'][0]['id']
        for tags in ['wrong', [7], ['x' * 61], ['x'] * 51, None]:
            self.assertEqual(self.post('/api/v2/media/tags', {'mediaId': media_id, 'tags': tags})[0], 400)
        from director_v2.catalog import update_tags
        from director_v2.database import connect
        connection = connect(self.db_path)
        try:
            with self.assertRaises(ValueError):
                update_tags(connection, media_id, 999, ['foreign'])
        finally:
            connection.close()
        self.post('/api/v2/media/trash', {'mediaId': media_id})
        self.assertEqual(self.post('/api/v2/media/tags', {'mediaId': media_id, 'tags': ['trash']})[0], 400)

    def test_sync_indexes_files_in_place_and_is_idempotent(self):
        original = {
            path: (path.stat().st_size, path.read_bytes())
            for path in (self.input_root / "林默-正面.png", self.output_root / "shot_001.mp4")
        }
        status, first = self.post("/api/v2/catalog/sync")
        self.assertEqual(status, 200)
        self.assertEqual(first["seen"], 2)
        self.assertEqual(first["created"], 2)

        status, second = self.post("/api/v2/catalog/sync")
        self.assertEqual(status, 200)
        self.assertEqual(second["created"], 0)
        status, listing = self.api.get("/api/v2/media", self.user)
        self.assertEqual(status, 200)
        self.assertEqual(listing["total"], 2)
        self.assertEqual(len(list(self.input_root.iterdir())), 1)
        self.assertEqual(len(list(self.output_root.iterdir())), 1)
        for path, (size, content) in original.items():
            self.assertEqual(path.stat().st_size, size)
            self.assertEqual(path.read_bytes(), content)

    def test_same_media_links_to_two_projects_without_copy(self):
        self.post("/api/v2/catalog/sync")
        # Direct class calls receive the query separately, mirroring the server delegation.
        _, listing = self.api.get("/api/v2/media", self.user, "kind=image")
        media_id = listing["items"][0]["id"]
        projects = []
        for name in ("夜班列车", "归途"):
            status, payload = self.post("/api/v2/projects", {"name": name})
            self.assertEqual(status, 201)
            projects.append(payload["project"]["id"])
        for project_id in projects:
            status, _ = self.post("/api/v2/media/link", {
                "mediaId": media_id, "targetType": "project",
                "targetId": project_id, "role": "reference",
            })
            self.assertEqual(status, 201)

        before_count = sum(1 for path in self.root.rglob("*") if path.is_file())
        status, promoted = self.post("/api/v2/media/promote", {"mediaId": media_id})
        after_count = sum(1 for path in self.root.rglob("*") if path.is_file())
        self.assertEqual(status, 200)
        self.assertFalse(promoted["copied"])
        self.assertEqual(promoted["bytesBefore"], promoted["bytesAfter"])
        self.assertEqual(before_count, after_count)

        connection = sqlite3.connect(self.db_path)
        link_count = connection.execute(
            "SELECT COUNT(*) FROM asset_links_v2 WHERE media_id=?", (media_id,)
        ).fetchone()[0]
        media_count = connection.execute(
            "SELECT COUNT(*) FROM media_assets_v2 WHERE id=?", (media_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(link_count, 3)
        self.assertEqual(media_count, 1)

    def test_bulk_media_actions_and_protected_permanent_delete(self):
        self.post('/api/v2/catalog/sync')
        _, listing = self.api.get('/api/v2/media', self.user)
        media_ids = [item['id'] for item in listing['items']]
        _, project = self.post('/api/v2/projects', {'name': '批量整理'})
        project_id = project['project']['id']

        status, linked = self.post('/api/v2/media/bulk', {
            'action': 'link_project', 'mediaIds': media_ids, 'projectId': project_id,
        })
        self.assertEqual(status, 200)
        self.assertEqual(linked['succeeded'], 2)
        status, trashed = self.post('/api/v2/media/bulk', {
            'action': 'trash', 'mediaIds': media_ids,
        })
        self.assertEqual(status, 200)
        self.assertEqual(trashed['succeeded'], 2)
        self.assertEqual(self.api.get('/api/v2/media', self.user, 'state=trash')[1]['total'], 2)
        status, protected = self.post('/api/v2/media/bulk', {
            'action': 'permanent_delete', 'mediaIds': media_ids,
        })
        self.assertEqual(status, 200)
        self.assertEqual(protected['succeeded'], 0)
        self.assertEqual(protected['failed'], 2)

        status, restored = self.post('/api/v2/media/bulk', {
            'action': 'restore', 'mediaIds': media_ids,
        })
        self.assertEqual(status, 200)
        self.assertEqual(restored['succeeded'], 2)

        disposable = self.input_root / '可彻底删除.png'
        disposable.write_bytes(b'disposable')
        self.post('/api/v2/catalog/sync')
        _, disposable_listing = self.api.get('/api/v2/media', self.user, 'q=可彻底删除')
        disposable_id = disposable_listing['items'][0]['id']
        self.post('/api/v2/media/bulk', {'action': 'trash', 'mediaIds': [disposable_id]})
        status, deleted = self.post('/api/v2/media/bulk', {
            'action': 'permanent_delete', 'mediaIds': [disposable_id],
        })
        self.assertEqual(status, 200)
        self.assertEqual(deleted['succeeded'], 1)
        self.assertFalse(disposable.exists())
        self.assertEqual(self.api.get('/api/v2/media', self.user, 'state=trash&q=可彻底删除')[1]['total'], 0)

    def test_classification_is_draft_then_edit_confirm_archive(self):
        self.post("/api/v2/catalog/sync")
        _, listing = self.api.get("/api/v2/media", self.user, "kind=image")
        media_id = listing["items"][0]["id"]
        status, entity = self.post("/api/v2/entities", {
            "type": "character", "name": "林默",
        })
        self.assertEqual(status, 201)
        character_id = entity["entity"]["id"]

        status, draft = self.post("/api/v2/classifications/draft", {
            "mediaId": media_id,
            "provider": "openai-compatible",
            "model": "vision-model",
            "classification": {
                "summary": "青年男性人物参考",
                "retrievalDescriptor": {"displayLabel": "林默 / 正面"},
                "links": [],
            },
        })
        self.assertEqual(status, 201)
        revision_id = draft["revisionId"]
        edited = {
            "summary": "林默，夜班制服，正面中景",
            "retrievalDescriptor": {
                "displayLabel": "林默 / 夜班制服 / 正面",
                "searchCaption": "用于夜间车站剧情的人物一致性参考",
                "bestFor": ["人物定妆", "正面镜头"],
                "avoidFor": ["古装剧情"],
                "usageNotes": "保持制服胸牌位置",
                "keywords": ["林默", "夜班", "制服", "正面"],
            },
            "links": [{
                "targetType": "character", "targetId": character_id,
                "role": "identity_reference",
            }],
        }
        status, archived = self.post("/api/v2/classifications/confirm", {
            "revisionId": revision_id, "classification": edited,
        })
        self.assertEqual(status, 200)
        self.assertEqual(archived["status"], "archived")
        status, detail = self.api.get(f"/api/v2/media/{media_id}", self.user)
        self.assertEqual(status, 200)
        self.assertEqual(detail["media"]["retrieval"]["displayLabel"], "林默 / 夜班制服 / 正面")
        self.assertEqual(detail["media"]["links"][0]["targetId"], character_id)

        status, failed = self.post("/api/v2/classifications/edit", {
            "revisionId": revision_id, "classification": edited,
        })
        self.assertEqual(status, 400)
        self.assertIn("不可修改", failed["error"])

    def test_public_share_exposes_preview_and_safe_editable_remix_only(self):
        self.post("/api/v2/catalog/sync")
        _, listing = self.api.get("/api/v2/media", self.user, "kind=image")
        media_id = listing["items"][0]["id"]
        _, draft = self.post("/api/v2/classifications/draft", {
            "mediaId": media_id,
            "classification": {
                "assetType": "character",
                "retrievalDescriptor": {
                    "displayLabel": "林默 / 夜班制服",
                    "searchCaption": "夜班车站人物参考",
                    "bestFor": ["人物一致性"], "avoidFor": [],
                    "usageNotes": "保留制服", "keywords": ["夜班"],
                },
                "links": [],
            },
        })
        self.post("/api/v2/classifications/confirm", {"revisionId": draft["revisionId"]})
        status, shared = self.post("/api/v2/shares/publish", {
            "mediaId": media_id, "title": "夜班人物参考",
            "recipe": {"generator": "image", "prompt": "雨夜站台", "aspect": "16:9"},
        })
        self.assertEqual(status, 201)
        self.assertFalse(shared["permissions"]["originalDownload"])

        status, discovery = self.api.public_get("/api/v2/discovery")
        self.assertEqual(status, 200)
        self.assertEqual(discovery["total"], 1)
        public_item = discovery["items"][0]
        self.assertNotIn("url", public_item)
        self.assertTrue(public_item["preview"].startswith("/api/v2/public-preview/"))

        status, remix = self.post("/api/v2/remix-drafts", {"shareId": shared["shareId"]})
        self.assertEqual(status, 201)
        self.assertFalse(remix["draft"]["submitted"])
        self.assertEqual(remix["draft"]["status"], "editable")
        encoded = str(remix).lower()
        self.assertNotIn("api_key", encoded)
        self.assertNotIn(str(self.root).lower(), encoded)

        status, favorite = self.post("/api/v2/shares/favorite", {"shareId": shared["shareId"]})
        self.assertEqual(status, 200)
        self.assertTrue(favorite["favorited"])
        status, report = self.post("/api/v2/shares/report", {
            "shareId": shared["shareId"], "reason": "misleading", "detail": "QA",
        })
        self.assertEqual(status, 201)
        self.assertTrue(report["reported"])

        status, _ = self.post("/api/v2/shares/withdraw", {"shareId": shared["shareId"]})
        self.assertEqual(status, 200)
        _, discovery = self.api.public_get("/api/v2/discovery")
        self.assertEqual(discovery["total"], 0)

    def test_project_analysis_reuses_entities_and_persists_shots(self):
        project_ids = []
        for name in ("项目甲", "项目乙"):
            status, created = self.post("/api/v2/projects", {"name": name, "aspect": "9:16"})
            self.assertEqual(status, 201)
            project_ids.append(created["project"]["id"])
        analysis = {
            "projectTitle": "夜班列车", "logline": "林默在雨夜站台追查真相", "aspect": "9:16",
            "characters": [{"name": "林默", "description": "28岁刑警", "visualPrompt": "黑色风衣"}],
            "scenes": [{"id": "scene_1", "name": "旧站台", "description": "雨夜", "visualPrompt": "空站台"}],
            "shots": [{"title": "抵达", "sceneId": "scene_1", "characters": ["林默"],
                       "duration": 3, "shotSize": "全景", "camera": "缓慢推进", "prompt": "雨夜抵达"}],
        }
        for project_id in project_ids:
            status, _ = self.post("/api/v2/projects/source", {
                "projectId": project_id, "sourceText": "这是用于测试的剧本文本，长度超过二十个字。",
                "sourceType": "script", "visualStyle": "电影感写实",
            })
            self.assertEqual(status, 200)
            status, applied = self.post("/api/v2/projects/apply-analysis", {
                "projectId": project_id, "analysis": analysis,
            })
            self.assertEqual(status, 200)
            # Reuse a resolved identity explicitly; a same-name match is not identity evidence.
            analysis["characters"][0]["entityId"] = applied["created"]["characters"][0]
            analysis["scenes"][0]["entityId"] = applied["created"]["locations"][0]
        status, first = self.api.get(f"/api/v2/projects/{project_ids[0]}", self.user)
        self.assertEqual(status, 200)
        self.assertEqual(len(first["shots"]), 1)
        self.assertEqual(first["shots"][0]["metadata"]["camera"], "缓慢推进")
        connection = sqlite3.connect(self.db_path)
        character_count = connection.execute(
            "SELECT COUNT(*) FROM entities_v2 WHERE entity_type='character' AND name='林默'"
        ).fetchone()[0]
        project_links = connection.execute(
            "SELECT COUNT(*) FROM entity_links_v2 WHERE role='project_use'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(character_count, 1)
        self.assertGreaterEqual(project_links, 4)  # character + location reused in both projects

    def test_generation_task_and_version_are_traceable(self):
        self.post("/api/v2/catalog/sync")
        _, media = self.api.get("/api/v2/media", self.user, "kind=image")
        media_id = media["items"][0]["id"]
        _, project = self.post("/api/v2/projects", {"name": "版本测试"})
        project_id = project["project"]["id"]
        status, task = self.post("/api/v2/tasks/create", {
            "kind": "image", "title": "人物参考 v2", "prompt": "雨夜人物",
            "parameters": {"seed": 42, "aspect": "16:9"}, "inputMedia": [],
            "count": 3, "projectId": project_id,
        })
        self.assertEqual(status, 201)
        status, marked = self.post("/api/v2/tasks/mark", {
            "taskId": task["id"], "status": "queued", "jobIds": ["job_1"],
            "parametersPatch": {"prefixes": ["test/run_1"]},
        })
        self.assertEqual(status, 200)
        status, version = self.post("/api/v2/versions/register", {
            "taskId": task["id"], "mediaId": media_id,
        })
        self.assertEqual(status, 201)
        status, detail = self.api.get(f"/api/v2/tasks/{task['id']}", self.user)
        self.assertEqual(status, 200)
        self.assertEqual(detail["status"], "running")
        self.assertEqual(detail["completedCount"], 1)
        self.assertEqual(detail["parameters"]["seed"], 42)
        self.assertEqual(detail["versions"][0]["mediaId"], media_id)

    def test_project_delete_is_reversible_and_keeps_media(self):
        status, created = self.post("/api/v2/projects", {"name": "可恢复项目"})
        self.assertEqual(status, 201)
        project_id = created["project"]["id"]
        self.post("/api/v2/catalog/sync")
        before = self.api.get("/api/v2/media", self.user)[1]["total"]
        status, deleted = self.post("/api/v2/projects/delete", {"projectId": project_id})
        self.assertEqual(status, 200)
        self.assertEqual(deleted["status"], "deleted")
        self.assertNotIn(project_id, [p["id"] for p in self.api.get("/api/v2/overview", self.user)[1]["projects"]])
        self.assertEqual(self.api.get("/api/v2/projects/trash", self.user)[1]["projects"][0]["id"], project_id)
        self.assertEqual(self.api.get("/api/v2/media", self.user)[1]["total"], before)
        status, restored = self.post("/api/v2/projects/restore", {"projectId": project_id})
        self.assertEqual((status, restored["status"]), (200, "active"))
        self.assertIn(project_id, [p["id"] for p in self.api.get("/api/v2/overview", self.user)[1]["projects"]])

    def test_project_with_active_task_cannot_be_deleted(self):
        _, created = self.post("/api/v2/projects", {"name": "运行中项目"})
        project_id = created["project"]["id"]
        _, task = self.post("/api/v2/tasks/create", {"kind": "video", "title": "处理中", "prompt": "测试", "count": 1, "projectId": project_id})
        self.post("/api/v2/tasks/mark", {"taskId": task["id"], "status": "queued", "jobIds": ["job-active"]})
        status, error = self.post("/api/v2/projects/delete", {"projectId": project_id})
        self.assertEqual(status, 400)
        self.assertIn("任务", error["error"])
        self.assertIn(project_id, [p["id"] for p in self.api.get("/api/v2/overview", self.user)[1]["projects"]])


if __name__ == "__main__":
    unittest.main()
