import copy
import json
import sqlite3
import unittest

import test_v2_catalog as catalog_tests
from director_v2.generation_contract import normalize_request
from director_v2.sharing import sanitize_recipe


class SelfcheckTests(unittest.TestCase):
    setUp = catalog_tests.DirectorV2CatalogTests.setUp
    tearDown = catalog_tests.DirectorV2CatalogTests.tearDown
    post = catalog_tests.DirectorV2CatalogTests.post

    def media(self):
        self.post('/api/v2/catalog/sync')
        return self.api.get('/api/v2/media', self.user, 'kind=image')[1]['items'][0]['id']

    def project(self):
        return self.post('/api/v2/projects', {'name': '测试项目'})[1]['project']['id']

    def task(self, **changes):
        payload = dict(kind='image', prompt='测试', parameters={}, inputMedia=[], count=3)
        payload.update(changes)
        status, result = self.post('/api/v2/tasks/create', payload)
        self.assertEqual(status, 201, result)
        return result['id']

    def archive(self, media_id, label):
        _, draft = self.post('/api/v2/classifications/draft', {'mediaId': media_id, 'classification': {
            'retrievalDescriptor': {'displayLabel': label, 'searchCaption': label}, 'links': [],
        }})
        status, archived = self.post('/api/v2/classifications/confirm', {'revisionId': draft['revisionId']})
        self.assertEqual(status, 200, archived)

    def test_share_pins_archive_until_explicit_republish(self):
        media_id = self.media()
        self.archive(media_id, '公开描述')
        _, shared = self.post('/api/v2/shares/publish', {'mediaId': media_id})
        self.archive(media_id, '尚未发布的修改')
        _, listing = self.api.public_get('/api/v2/discovery')
        self.assertEqual(listing['items'][0]['retrieval']['displayLabel'], '公开描述')
        _, search = self.api.public_get('/api/v2/discovery', 'q=尚未发布')
        self.assertEqual(search['total'], 0)
        _, remix = self.post('/api/v2/remix-drafts', {'shareId': shared['shareId']})
        self.assertEqual(remix['draft']['retrieval']['displayLabel'], '公开描述')
        self.post('/api/v2/shares/publish', {'mediaId': media_id})
        self.assertEqual(self.api.public_get('/api/v2/discovery')[1]['items'][0]['retrieval']['displayLabel'], '尚未发布的修改')

    def test_share_trash_hides_preview_and_disallows_remix(self):
        media_id = self.media()
        self.archive(media_id, '可公开')
        _, share = self.post('/api/v2/shares/publish', {'mediaId': media_id})
        self.post('/api/v2/media/trash', {'mediaId': media_id})
        self.assertEqual(self.api.public_get('/api/v2/discovery')[1]['total'], 0)
        self.assertEqual(self.post('/api/v2/remix-drafts', {'shareId': share['shareId']})[0], 400)
        with self.assertRaises(ValueError):
            self.api.public_preview_context(share['shareId'], input_root_for_user=lambda _: self.input_root, output_root_for_user=lambda _: self.output_root)

    def test_share_permissions_cannot_be_truthy_strings(self):
        media_id = self.media()
        self.archive(media_id, '测试')
        self.assertEqual(self.post('/api/v2/shares/publish', {'mediaId': media_id, 'permissions': {'remix': 'false'}})[0], 400)
        _, share = self.post('/api/v2/shares/publish', {'mediaId': media_id, 'permissions': {'platformUse': False}})
        self.assertEqual(self.post('/api/v2/remix-drafts', {'shareId': share['shareId']})[0], 400)

    def test_nested_secret_fields_are_not_in_recipe(self):
        clean = sanitize_recipe({'prompt': '雨夜', 'privateFields': ['a'], 'parameterValues': {
            'size': '1024x1024', 'apiKey': 'secret', 'provider': {'token': 'secret'}, 'localPath': 'D:/secret.png',
        }})
        self.assertEqual(clean['parameterValues'], {'size': '1024x1024'})
        self.assertNotIn('secret', json.dumps(clean))
        for value in ['D:/secret/image.png', 'Bearer abc', 'api_key=abc']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                sanitize_recipe({'prompt': value})
        with self.assertRaises(ValueError):
            sanitize_recipe({'parameterValues': {'seed': {'token': 'secret'}}})

    def test_embedding_provider_change_and_archive_change_invalidate_cache(self):
        from director_v2.semantic import pending_inputs, save_vectors, search
        media_id = self.media()
        self.archive(media_id, '旧描述')
        con, workspace = self.api._context(self.user)
        try:
            pending = pending_inputs(con, workspace['id'], 'same-model', provider='provider-a')
            save_vectors(con, 'provider-a', 'same-model', pending, [[1., 0.]])
            self.assertEqual(pending_inputs(con, workspace['id'], 'same-model', provider='provider-a'), [])
            self.assertEqual(len(pending_inputs(con, workspace['id'], 'same-model', provider='provider-b')), 1)
            self.assertEqual(search(con, workspace['id'], 'same-model', [1., 0.], provider='provider-b'), [])
            self.assertEqual(len(search(con, workspace['id'], 'same-model', [1., 0.], provider='provider-a')), 1)
            self.archive(media_id, '修改后描述')
            self.assertEqual(search(con, workspace['id'], 'same-model', [1., 0.], provider='provider-a'), [])
        finally:
            con.close()

    def test_invalid_embedding_values_cannot_corrupt_cache(self):
        from director_v2.semantic import pending_inputs, save_vectors, search
        media_id = self.media()
        self.archive(media_id, '描述')
        con, workspace = self.api._context(self.user)
        try:
            pending = pending_inputs(con, workspace['id'], 'model')
            for vector in [[float('nan')], [float('inf')], [True], ['1'], []]:
                with self.subTest(vector=vector), self.assertRaises(ValueError):
                    save_vectors(con, 'provider', 'model', pending, [vector])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM media_embeddings_v2').fetchone()[0], 0)
            with self.assertRaises(ValueError):
                search(con, workspace['id'], 'model', [float('nan')])
        finally:
            con.close()

    def test_partial_outputs_complete_only_at_requested_count(self):
        media_id = self.media()
        task_id = self.task()
        self.post('/api/v2/versions/register', {'taskId': task_id, 'mediaId': media_id})
        self.assertEqual(self.post('/api/v2/tasks/mark', {'taskId': task_id, 'status': 'completed'})[0], 400)
        for index in range(2):
            (self.output_root / f'extra{index}.png').write_bytes(b'image')
        self.post('/api/v2/catalog/sync')
        ids = [item['id'] for item in self.api.get('/api/v2/media', self.user, 'kind=image')[1]['items'] if item['id'] != media_id]
        for other_id in ids:
            self.assertEqual(self.post('/api/v2/versions/register', {'taskId': task_id, 'mediaId': other_id})[0], 201)
        _, task = self.api.get('/api/v2/tasks/' + task_id, self.user)
        self.assertEqual(task['completedCount'], 3)
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(self.post('/api/v2/tasks/mark', {'taskId': task_id, 'status': 'queued'})[0], 400)
        self.post('/api/v2/versions/register', {'taskId': task_id, 'mediaId': media_id})
        self.assertEqual(self.api.get('/api/v2/tasks/' + task_id, self.user)[1]['completedCount'], 3)

    def test_invalid_context_rejected_before_task_is_created(self):
        project_id = self.project()
        _, shot = self.post('/api/v2/entities', {'type': 'shot', 'name': '镜头', 'projectId': project_id})
        cases = [{'projectId': 'foreign'}, {'shotId': shot['entity']['id']},
                 {'inputMedia': ['foreign']}, {'parameters': {'parentVersionId': 'foreign'}},
                 {'parameters': {'entityId': 'foreign', 'entityType': 'character'}}]
        for changes in cases:
            payload = dict(kind='image', count=1, parameters={}, inputMedia=[])
            payload.update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.post('/api/v2/tasks/create', payload)[0], 400)
        self.assertEqual(self.api.get('/api/v2/tasks', self.user)[1]['tasks'], [])

    def test_generated_entity_and_project_links_saved_by_backend(self):
        media_id, project_id = self.media(), self.project()
        _, entity = self.post('/api/v2/entities', {'type': 'character', 'name': '角色'})
        task_id = self.task(projectId=project_id, parameters={'entityId': entity['entity']['id'], 'entityType': 'character'})
        self.assertEqual(self.post('/api/v2/versions/register', {'taskId': task_id, 'mediaId': media_id})[0], 201)
        links = self.api.get('/api/v2/media/' + media_id, self.user)[1]['media']['links']
        self.assertEqual({link['targetType'] for link in links}, {'project', 'character'})

    def test_same_name_is_not_identity_and_explicit_reuse_preserves_description(self):
        analysis = {'characters': [{'name': '林默', 'description': '原设定'}], 'scenes': [], 'shots': []}
        _, first = self.post('/api/v2/projects/apply-analysis', {'projectId': self.project(), 'analysis': analysis})
        _, second = self.post('/api/v2/projects/apply-analysis', {'projectId': self.project(), 'analysis': analysis})
        self.assertNotEqual(first['created']['characters'][0], second['created']['characters'][0])
        analysis['characters'][0].update(entityId=first['created']['characters'][0], description='不能覆盖')
        _, reused = self.post('/api/v2/projects/apply-analysis', {'projectId': self.project(), 'analysis': analysis})
        self.assertEqual(reused['created']['characters'], first['created']['characters'])
        entities = self.api.get('/api/v2/entities', self.user)[1]['entities']
        self.assertTrue(all(entity['metadata']['description'] == '原设定' for entity in entities))

    def test_project_detail_tolerates_project_bound_character_and_counts_active_shots(self):
        project_id = self.project()
        self.post('/api/v2/entities', {'projectId': project_id, 'type': 'character', 'name': '旧人物'})
        manual = self.post('/api/v2/entities', {'projectId': project_id, 'type': 'shot', 'name': '手工分镜'})[1]['entity']['id']
        for _ in range(2):
            self.post('/api/v2/projects/apply-analysis', {'projectId': project_id, 'analysis': {'shots': [{'title': 'AI镜头'}]}})
        status, detail = self.api.get('/api/v2/projects/' + project_id, self.user)
        self.assertEqual(status, 200)
        self.assertEqual(len(detail['characters']), 1)
        self.assertIn(manual, [shot['id'] for shot in detail['shots']])
        self.assertEqual(len(detail['shots']), 2)
        self.assertEqual(self.api.get('/api/v2/overview', self.user)[1]['projects'][0]['counts']['shot'], 2)

    def test_shot_approval_requires_real_version_and_rejection_clears_selection(self):
        media_id, project_id = self.media(), self.project()
        shot_id = self.post('/api/v2/entities', {'projectId': project_id, 'type': 'shot', 'name': '镜头'})[1]['entity']['id']
        self.assertEqual(self.post('/api/v2/entities/update', {'entityId': shot_id, 'metadata': {'status': 'approved'}})[0], 400)
        task_id = self.task(projectId=project_id, shotId=shot_id)
        _, version = self.post('/api/v2/versions/register', {'taskId': task_id, 'mediaId': media_id})
        request = dict(shotId=shot_id, versionId=version['versionId'])
        self.assertEqual(self.post('/api/v2/versions/state', dict(request, state='approved'))[0], 200)
        self.assertEqual(self.post('/api/v2/versions/state', dict(request, state='rejected'))[0], 200)
        metadata = self.api.get('/api/v2/projects/' + project_id, self.user)[1]['shots'][0]['metadata']
        self.assertIsNone(metadata['selectedVersionId'])
        self.assertEqual(metadata['status'], 'draft')


class GenerationContractTests(unittest.TestCase):
    def test_only_effective_parameters_are_recorded(self):
        req, params = normalize_request(dict(kind='image', prompt='测试', size='auto', seed=42, negative='ignored', strength=.5))
        self.assertEqual(params, {'size': 'auto', 'mediaIds': []})
        self.assertNotIn('seed', req)
        self.assertNotIn('strength', req)

    def test_seed_zero_aspect_and_frame_references_are_retained(self):
        req, params = normalize_request(dict(kind='video', mode='i2v', prompt='测试', seed=0, aspect='9:16', firstFrameId='a', lastFrameId='b'))
        self.assertEqual(params['seed'], 0)
        self.assertEqual(params['aspect'], '9:16 (Portrait Widescreen)')
        self.assertEqual(req['mediaIds'], ['a', 'b'])

    def test_invalid_count_or_unsupported_reference_fails_explicitly(self):
        cases = [{'count': 0}, {'count': True}, {'count': 1.5}, {'count': 9}, {'mediaIds': 'bad'},
                 {'kind': 'image', 'mediaIds': ['ref']}, {'kind': 'image_edit', 'mediaIds': []},
                 {'duration': float('nan')}, {'duration': 3}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                normalize_request(dict({'prompt': '测试'}, **values))


if __name__ == '__main__':
    unittest.main()
