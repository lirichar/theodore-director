from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from director_v2 import catalog, classification
from director_v2.database import connect, ensure_personal_workspace


class CatalogIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.output = self.root / "output"
        self.input.mkdir()
        self.output.mkdir()
        self.path = self.output / "result.png"
        self.path.write_bytes(b"canonical-image")
        self.db = self.root / "test.db"
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,role TEXT)")
        connection.executemany("INSERT INTO users VALUES(?,?,?)", [(1, "owner", "user"), (2, "other", "user")])
        connection.commit()
        connection.close()
        self.con = connect(self.db)
        self.user = {"id": 1, "username": "owner", "role": "user"}
        self.workspace = ensure_personal_workspace(self.con, self.user)["id"]
        self.sync()
        self.media_id = catalog.deterministic_media_id(1, "output", "result.png")

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def sync(self):
        return catalog.sync_legacy_catalog(self.con, self.user, self.workspace, self.input, self.output)

    def media(self):
        return self.con.execute("SELECT * FROM media_assets_v2 WHERE id=?", (self.media_id,)).fetchone()

    def draft(self, links=None):
        return classification.create_draft(self.con, self.user, self.workspace, self.media_id, {
            "retrievalDescriptor": {"displayLabel": "参考图"}, "links": links or [],
        })

    def entity(self):
        self.con.execute("INSERT INTO entities_v2(id,workspace_id,owner_user_id,entity_type,name,created_at,updated_at) VALUES('character_1',?,1,'character','林默',1,1)", (self.workspace,))
        self.con.commit()
        return {"targetType": "character", "targetId": "character_1", "role": "identity_reference"}

    def test_sync_preserves_name_source_trash_and_metadata_time(self):
        catalog.update_display_name(self.con, self.media_id, 1, "自定义的素材名")
        self.con.execute("UPDATE media_assets_v2 SET source_kind='generation',updated_at=9999999999 WHERE id=?", (self.media_id,))
        self.con.commit()
        self.sync()
        self.assertEqual(self.media()["display_name"], "自定义的素材名")
        self.assertEqual(self.media()["source_kind"], "generation")
        self.assertEqual(self.media()["updated_at"], 9999999999)
        catalog.set_lifecycle(self.con, self.media_id, 1, "trash")
        deleted_at = self.media()["deleted_at"]
        self.sync()
        self.assertEqual(self.media()["lifecycle_state"], "trash")
        self.assertEqual(self.media()["deleted_at"], deleted_at)

    def test_internal_reference_alias_is_not_a_second_asset(self):
        alias = self.input / f"_ref_{self.media_id}.png"
        alias.hardlink_to(self.path)
        result = self.sync()
        self.assertEqual(result["seen"], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM media_assets_v2 WHERE lifecycle_state='active'").fetchone()[0], 1)

    def test_generation_file_becomes_missing_and_can_return(self):
        self.con.execute("UPDATE media_assets_v2 SET source_kind='generation' WHERE id=?", (self.media_id,))
        self.con.commit()
        self.path.unlink()
        self.sync()
        self.assertEqual(self.media()["lifecycle_state"], "missing")
        self.path.write_bytes(b"canonical-image")
        self.sync()
        self.assertEqual(self.media()["lifecycle_state"], "active")

    def test_hash_duplicates_do_not_disclose_other_users(self):
        other = {"id": 2, "username": "other", "role": "user"}
        other_workspace = ensure_personal_workspace(self.con, other)["id"]
        catalog.sync_legacy_catalog(self.con, other, other_workspace, self.input, self.output)
        other_id = catalog.deterministic_media_id(2, "output", "result.png")
        catalog.compute_hash(self.con, other_id, 2, self.input, self.output)
        result = catalog.compute_hash(self.con, self.media_id, 1, self.input, self.output)
        self.assertEqual(result["duplicates"], [])

    def test_permanent_delete_database_failure_preserves_bytes(self):
        catalog.set_lifecycle(self.con, self.media_id, 1, "trash")
        self.con.execute("CREATE TRIGGER prevent_delete BEFORE DELETE ON media_assets_v2 BEGIN SELECT RAISE(ABORT, 'injected SQL failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            catalog.permanent_delete(self.con, self.media_id, 1, self.input, self.output)
        self.assertTrue(self.path.exists())
        self.assertIsNotNone(self.media())

    def test_permanent_delete_respects_delivery_reference(self):
        self.con.execute("INSERT INTO projects_v2(id,workspace_id,owner_user_id,name,created_at,updated_at) VALUES('project_1',?,1,'成片',1,1)", (self.workspace,))
        self.con.execute("INSERT INTO deliveries_v2(id,project_id,media_id,name,created_by,created_at,updated_at) VALUES('delivery_1','project_1',?,'交付',1,1,1)", (self.media_id,))
        self.con.commit()
        catalog.set_lifecycle(self.con, self.media_id, 1, "trash")
        with self.assertRaises(ValueError):
            catalog.permanent_delete(self.con, self.media_id, 1, self.input, self.output)
        self.assertTrue(self.path.exists())

    def test_permanent_delete_requires_trash(self):
        with self.assertRaises(ValueError):
            catalog.permanent_delete(self.con, self.media_id, 1, self.input, self.output)
        self.assertTrue(self.path.exists())

    def test_confirm_preserves_existing_manual_link_and_deduplicates(self):
        link = self.entity()
        catalog.link_media(self.con, self.media_id, link["targetType"], link["targetId"], link["role"], 1)
        draft = self.draft([link, link])
        result = classification.confirm_archive(self.con, self.user, self.workspace, draft["revisionId"])
        self.assertEqual(result["status"], "archived")
        links = self.con.execute("SELECT * FROM asset_links_v2 WHERE media_id=?", (self.media_id,)).fetchall()
        self.assertEqual(len(links), 1)
        self.assertIsNone(links[0]["classification_revision_id"])

    def test_archived_link_cannot_be_removed_outside_revision(self):
        draft = self.draft([self.entity()])
        classification.confirm_archive(self.con, self.user, self.workspace, draft["revisionId"])
        link_id = self.con.execute("SELECT id FROM asset_links_v2 WHERE media_id=?", (self.media_id,)).fetchone()[0]
        with self.assertRaises(ValueError):
            catalog.unlink_media(self.con, self.media_id, 1, link_id)

    def test_invalid_confirmation_rolls_back_every_part(self):
        draft = self.draft([{"targetType": "character", "targetId": "missing", "role": "identity_reference"}])
        with self.assertRaises(ValueError):
            classification.confirm_archive(self.con, self.user, self.workspace, draft["revisionId"])
        row = self.con.execute("SELECT state FROM classification_revisions_v2 WHERE id=?", (draft["revisionId"],)).fetchone()
        self.assertEqual(row["state"], "draft")
        self.assertEqual(json.loads(self.media()["retrieval_json"]), {})

    def test_rerun_keeps_archive_immutable(self):
        archived = self.draft()
        classification.confirm_archive(self.con, self.user, self.workspace, archived["revisionId"])
        original = self.con.execute("SELECT data_json FROM classification_revisions_v2 WHERE id=?", (archived["revisionId"],)).fetchone()[0]
        newer = self.draft()
        with self.assertRaises(ValueError):
            classification.update_draft(self.con, self.user, archived["revisionId"], {"summary": "不能覆盖"})
        self.assertEqual(self.con.execute("SELECT data_json FROM classification_revisions_v2 WHERE id=?", (archived["revisionId"],)).fetchone()[0], original)
        self.assertEqual(classification.list_revisions(self.con, self.user, self.media_id)[0]["revisionId"], newer["revisionId"])


if __name__ == "__main__":
    unittest.main()
