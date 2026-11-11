"""Isolated HTTP/provider-contract checks; never use real keys, models or user files."""

import base64
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from PIL import Image
from fixture_graphs import write_character_graph, write_qwen_graph


class HttpSelfcheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='theodore_http_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = {'THEODORE_AUTH_DB': str(self.root / 'auth.db'), 'THEODORE_COMFY_ROOT': str(self.root / 'comfy'),
               'THEODORE_INPUT_ROOT': str(self.root / 'comfy/input/theodore_mobile'),
               'THEODORE_OUTPUT_ROOT': str(self.root / 'comfy/output'), 'THEODORE_JOBS_FILE': str(self.root / 'jobs.json'),
               'THEODORE_THUMBS': str(self.root / 'thumbs'), 'THEODORE_WORKFLOW_DIR': str(self.root),
               'THEODORE_CHARACTER_WORKFLOW': str(self.root / 'character.json')}
        spec = importlib.util.spec_from_file_location('theodore_http_test_server', Path(__file__).parents[1] / 'server.py')
        self.app = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, env):
            spec.loader.exec_module(self.app)
        con = self.app.db()
        salt = b'test-only-salt-16'
        for uid, role in ((1, 'admin'), (2, 'user')):
            con.execute('INSERT INTO users(id,username,password,salt,role,created_at) VALUES(?,?,?,?,?,1)',
                        (uid, f'qa_{uid}', self.app.password_hash('test-only-password', salt), salt, role))
        con.commit()
        con.close()
        self.user = {'id': 1, 'username': 'qa_1', 'role': 'admin'}
        self.other = {'id': 2, 'username': 'qa_2', 'role': 'user'}
        buffer = io.BytesIO()
        Image.new('RGB', (32, 32), '#405060').save(buffer, 'PNG')
        self.png = buffer.getvalue()
        self.app.INPUT.joinpath('fixture.png').write_bytes(self.png)
        self.app.result_root(self.user)
        self.app.V2_API.post('/api/v2/catalog/sync', self.user, {}, input_root=self.app.INPUT, output_root=self.app.OUTPUT)
        self.media_id = self.app.V2_API.get('/api/v2/media', self.user)[1]['items'][0]['id']
        class Handler(self.app.H):
            def log_message(self, *args):
                pass
        self.http = self.app.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie, self.csrf = '', ''
        status, headers, data = self.request('/api/auth/login', {'username': 'qa_1', 'password': 'test-only-password'})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.csrf = data['csrf']

    def close_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None, *, auth=True, csrf=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=5)
        headers = {'Content-Type': 'application/json'}
        if auth and self.cookie:
            headers['Cookie'] = self.cookie
        if csrf:
            headers['X-CSRF-Token'] = self.csrf
        connection.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data, status, received = response.read(), response.status, dict(response.getheaders())
        connection.close()
        if received.get('Content-Type', '').startswith('application/json'):
            data = json.loads(data)
        return status, received, data

    def test_auth_redirect_and_script_injection_boundary(self):
        status, headers, _ = self.request('/studio.html', auth=False)
        self.assertEqual(status, 302)
        self.assertIn('/login.html?next=', headers['Location'])
        for target in ['/\\evil.test', '//evil.test', '/</script><script>alert(1)</script>']:
            status, _, data = self.request('/login.html?next=' + quote(target, safe=''), auth=False)
            self.assertEqual(status, 200)
            self.assertNotIn(b'alert(1)', data)
            self.assertNotIn(b'evil.test', data)
        self.assertEqual(self.request('/api/v2/bootstrap', auth=False)[0], 401)

    def test_studio_is_the_only_director_entrypoint(self):
        for path in ['/', '/studio.html', '/assets.html', '/account.html', '/settings.html', '/discover.html', '/studio-workflow.js']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 200)
        status, _, root = self.request('/')
        self.assertIn(b'/studio-workflow.js', root)
        self.assertNotIn(b'/app.js', root)
        self.assertNotIn('返回原导演台'.encode(), root)
        status, headers, _ = self.request('/index.html')
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/studio.html')
        for path in ['/app.js', '/studio.css', '/frame.css']:
            self.assertEqual(self.request(path)[0], 404)
        for path in ['/', '/login.html', '/login.html?next=/']:
            status, _, login = self.request(path, auth=False)
            self.assertEqual(status, 200)
            self.assertIn(b'location.href="/studio.html"', login)
        status, _, login = self.request('/login.html?next=/discover.html', auth=False)
        self.assertIn(b'location.href="/discover.html"', login)
        self.assertEqual(self.request('/api/library')[0], 200)
        self.assertEqual(self.request('/api/results')[0], 200)

    def test_http_cannot_forge_worker_status_and_requires_csrf(self):
        self.assertEqual(self.request('/api/v2/tasks/mark', {'taskId': 'fake', 'jobIds': ['someone-else']})[0], 403)
        self.assertEqual(self.request('/api/v2/versions/register', {})[0], 403)
        self.assertEqual(self.request('/api/v2/projects', {'name': 'bad'}, csrf=False)[0], 403)
        self.assertEqual(self.request('/api/v2/projects', [1, 2])[0], 400)

    def test_other_user_media_and_tasks_cannot_be_operated(self):
        inputs, outputs = self.app.user_input(self.other), self.app.result_root(self.other)
        inputs.joinpath('other.png').write_bytes(self.png)
        roots = dict(input_root=inputs, output_root=outputs)
        self.app.V2_API.post('/api/v2/catalog/sync', self.other, {}, **roots)
        media_id = self.app.V2_API.get('/api/v2/media', self.other)[1]['items'][0]['id']
        _, task = self.app.V2_API.post('/api/v2/tasks/create', self.other, dict(kind='image', count=1), **roots)
        with patch.object(self.app, 'ai_request') as remote, patch.object(self.app, 'comfy_post') as comfy:
            self.assertEqual(self.request('/api/v2/media/' + media_id)[0], 400)
            self.assertEqual(self.request('/api/v2/classifications/run', {'mediaId': media_id})[0], 400)
            self.assertEqual(self.request('/api/v2/generation/cancel', {'taskId': task['id']})[0], 400)
            self.assertEqual(self.request('/api/v2/media/promote', {'mediaId': media_id})[0], 400)
        remote.assert_not_called()
        comfy.assert_not_called()

    def test_missing_provider_returns_editable_manual_draft(self):
        with patch.object(self.app, 'ai_request') as remote:
            status, _, result = self.request('/api/v2/classifications/run', {'mediaId': self.media_id})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['status'], 'draft')
        self.assertEqual(result['classification']['links'], [])
        remote.assert_not_called()

    def test_idle_task_read_does_not_scan_filesystem_or_call_comfy(self):
        with patch.object(self.app, 'comfy_get') as comfy, patch.object(self.app.V2_API, 'post') as post:
            self.app.reconcile_v2_tasks(self.user)
        comfy.assert_not_called()
        post.assert_not_called()

    def test_reference_alias_cannot_silently_select_wrong_file(self):
        path = self.app.OUTPUT / 'result.png'
        path.write_bytes(self.png)
        roots = dict(input_root=self.app.INPUT, output_root=self.app.OUTPUT)
        self.app.V2_API.post('/api/v2/catalog/sync', self.user, {}, **roots)
        media_id = next(item['id'] for item in self.app.V2_API.get('/api/v2/media', self.user)[1]['items'] if item['name'] == 'result')
        alias = self.app.INPUT / f'_ref_{media_id}.png'
        alias.write_bytes(b'wrong-file')
        with self.assertRaises(ValueError):
            self.app.v2_reference_name(media_id, self.user)
        self.assertEqual(alias.read_bytes(), b'wrong-file')

    def test_partial_image_response_keeps_output_and_reports_incomplete(self):
        config = dict(api_key='synthetic-test-key', image_model='test-model', image_size='1024x1024')
        with patch.object(self.app, 'ai_config', return_value=config), patch.object(self.app, 'ai_request', return_value={'data': [{'b64_json': base64.b64encode(self.png).decode()}]}) as remote:
            result = self.app.submit_v2_generation({'kind': 'image', 'prompt': 'fixture', 'count': 2, 'size': '1024x1024'}, self.user)
        self.assertEqual(result['status'], 'failed')
        task = self.app.V2_API.get('/api/v2/tasks/' + result['taskId'], self.user)[1]
        self.assertEqual(task['completedCount'], 1)
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['parameters']['size'], '1024x1024')
        self.assertEqual(remote.call_args.args[2]['n'], 2)

    def test_two_batch_video_outputs_reconcile_for_admin_and_user(self):
        for user in (self.user, self.other):
            self.app.user_input(user)
            self.app.result_root(user)
            counter = iter(['job_1', 'job_2'])
            with patch.object(self.app, 'workflow', side_effect=lambda *_: ({'92': {'inputs': {}}}, 'same_old_prefix', 5)), patch.object(self.app, 'comfy_post', side_effect=lambda *_: {'prompt_id': next(counter)}):
                result = self.app.submit_v2_generation({'kind': 'video', 'prompt': 'fixture', 'count': 2, 'seed': 0}, user)
            task = self.app.V2_API.get('/api/v2/tasks/' + result['taskId'], user)[1]
            prefixes = task['parameters']['prefixes']
            self.assertEqual(len(set(prefixes)), 2)
            self.assertTrue(all(result['taskId'] in prefix for prefix in prefixes))
            for index, prefix in enumerate(prefixes):
                path = self.app.OUTPUT / (prefix + '_00001.mp4')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture-video')
                with patch.object(self.app, 'comfy_get', return_value={'queue_running': [['number', 'job_2']], 'queue_pending': []}):
                    self.app.reconcile_v2_tasks(user)
                task = self.app.V2_API.get('/api/v2/tasks/' + result['taskId'], user)[1]
                self.assertEqual(task['completedCount'], index + 1)
                self.assertEqual(task['status'], 'running' if index == 0 else 'completed')

    def test_legacy_api_save_preserves_new_optional_models(self):
        values = {'baseUrl': 'http://configured.test/v1', 'apiKey': 'synthetic-test-key', 'textModel': 'text', 'imageModel': 'image', 'classificationModel': 'vision', 'embeddingModel': 'embedding'}
        self.assertEqual(self.request('/api/ai-config', values)[0], 200)
        values.pop('classificationModel')
        values.pop('embeddingModel')
        self.assertEqual(self.request('/api/ai-config', values)[0], 200)
        cfg = self.request('/api/ai-config')[2]
        self.assertEqual(cfg['classificationModel'], 'vision')
        self.assertEqual(cfg['embeddingModel'], 'embedding')
        self.assertNotIn('apiKey', cfg)
        values['embeddingModel'] = ''
        self.assertEqual(self.request('/api/ai-config', values)[0], 200)
        with patch.object(self.app, 'ai_request') as remote:
            self.assertEqual(self.app.run_v2_semantic_search({'query': 'fixture'}, self.user)['method'], 'keyword_fallback')
        remote.assert_not_called()

    def test_workflow_catalog_is_authenticated(self):
        self.assertEqual(self.request('/api/v2/workflows', auth=False)[0], 401)
        status, _, result = self.request('/api/v2/workflows')
        self.assertEqual(status, 200)
        self.assertEqual({workflow['id'] for workflow in result['workflows']}, {'qwen_image_edit', 'character_replace'})

    def test_legacy_history_route_is_authenticated_and_owner_filtered(self):
        self.app.JOBS.write_text(json.dumps({
            'mine': {'user_id': 1, 'title': '我的旧记录', 'prompt': '旧提示词'},
            'other': {'user_id': 2, 'title': '他人旧记录'},
            'mapped': {'user_id': 1, 'title': '已映射新版', 'v2_task_id': 'task_x'},
        }), encoding='utf-8')
        self.assertEqual(self.request('/api/v2/legacy-history', auth=False)[0], 401)
        status, _, result = self.request('/api/v2/legacy-history?limit=30&offset=0')
        self.assertEqual(status, 200)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['title'], '我的旧记录')
        self.assertEqual(result['items'][0]['status'], 'unknown')

    def test_character_workflow_submits_selected_inputs_without_copy(self):
        write_character_graph(self.app.CHARACTER_WORKFLOW)
        source = self.app.INPUT / 'source.mp4'
        source.write_bytes(b'fixture-video')
        roots = dict(input_root=self.app.INPUT, output_root=self.app.OUTPUT)
        self.app.V2_API.post('/api/v2/catalog/sync', self.user, {}, **roots)
        video_id = next(item['id'] for item in self.app.V2_API.get('/api/v2/media', self.user)[1]['items'] if item['kind'] == 'video')
        graphs = []
        def submit(path, payload):
            graphs.append(payload['prompt'])
            return {'prompt_id': 'replacement_job_' + str(len(graphs))}
        with patch.object(self.app, 'missing_dependencies', return_value=[]), patch.object(self.app, 'validate_local_graph_nodes'), patch.object(self.app, 'comfy_post', side_effect=submit):
            result = self.app.submit_v2_generation(dict(kind='video', workflowId='character_replace', prompt='使用所选角色',
                sourceVideoId=video_id, characterMediaId=self.media_id, count=2, seed=0, preserveAudio=False), self.user)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(len(graphs), 2)
        self.assertEqual(graphs[0]['source_video']['inputs']['file'], 'theodore_mobile/source.mp4')
        self.assertEqual(graphs[0]['replace_character']['inputs']['image'], 'theodore_mobile/fixture.png')
        self.assertEqual(graphs[0]['129']['inputs']['noise_seed'], 0)
        self.assertEqual(graphs[1]['129']['inputs']['noise_seed'], 1)
        self.assertNotIn('audio', graphs[0]['130']['inputs'])
        self.assertEqual(len(list(self.app.INPUT.iterdir())), 2)
        task = self.app.V2_API.get('/api/v2/tasks/' + result['taskId'], self.user)[1]
        self.assertEqual(task['parameters']['workflowId'], 'character_replace')
        self.assertEqual(task['inputMedia'], [video_id, self.media_id])

    def test_qwen_adapter_wires_all_selected_images_and_removes_demo(self):
        write_qwen_graph(self.app.API_DIR / 'qwen_image_edit_2511_api.json')
        for name in ['second.png', 'third.png']:
            (self.app.INPUT / name).write_bytes(self.png)
        graph, _ = self.app.qwen_workflow(dict(selected=['fixture.png', 'second.png', 'third.png'],
            prompt='统一光线', negative='不要改变身份', seed=0), self.user)
        positive = graph['170:151']['inputs']
        self.assertEqual(positive['prompt'], '统一光线')
        self.assertEqual(graph['170:149']['inputs']['prompt'], '不要改变身份')
        self.assertEqual(graph['170:169']['inputs']['seed'], 0)
        self.assertEqual(len([key for key in positive if key.startswith('image')]), 3)
        self.assertNotIn('lily_girl_blue.png', json.dumps(graph))

    def test_dependency_failure_does_not_submit_task(self):
        with patch.object(self.app, 'comfy_post') as queue:
            status, _, result = self.request('/api/v2/generation/submit', dict(kind='image_edit', prompt='编辑', mediaIds=[self.media_id]))
        self.assertEqual(status, 400)
        self.assertIn('依赖', result['error'])
        queue.assert_not_called()
        self.assertEqual(self.app.V2_API.get('/api/v2/tasks', self.user)[1]['tasks'], [])

    def test_reconcile_marks_provider_error_even_if_queue_still_says_running(self):
        roots = dict(input_root=self.app.INPUT, output_root=self.app.OUTPUT)
        _, task = self.app.V2_API.post('/api/v2/tasks/create', self.user, {
            'kind': 'video', 'title': '显存失败', 'prompt': 'fixture',
            'parameters': {'prefixes': ['TheodoreMobile/test-oom']}, 'inputMedia': [], 'count': 1,
        }, **roots)
        self.app.V2_API.post('/api/v2/tasks/mark', self.user, {
            'taskId': task['id'], 'status': 'queued', 'jobIds': ['oom-job'],
        }, **roots)
        def comfy(path):
            if path == '/queue':
                return {'queue_running': [[1, 'oom-job']], 'queue_pending': []}
            return {'oom-job': {'status': {'status_str': 'error', 'completed': False,
                'messages': [['execution_error', {'exception_message': 'CUDA out of memory'}]]}}}
        with patch.object(self.app, 'comfy_get', side_effect=comfy):
            self.app.reconcile_v2_tasks(self.user)
        detail = self.app.V2_API.get('/api/v2/tasks/' + task['id'], self.user)[1]
        self.assertEqual(detail['status'], 'failed')
        self.assertIn('CUDA out of memory', detail['error'])

    def test_user_can_end_a_stalled_task_without_resubmitting_it(self):
        roots = dict(input_root=self.app.INPUT, output_root=self.app.OUTPUT)
        _, task = self.app.V2_API.post('/api/v2/tasks/create', self.user, {
            'kind': 'video', 'title': '卡住的任务', 'prompt': 'fixture',
            'parameters': {}, 'inputMedia': [], 'count': 1,
        }, **roots)
        self.app.V2_API.post('/api/v2/tasks/mark', self.user, {
            'taskId': task['id'], 'status': 'queued', 'jobIds': ['stalled-job'],
        }, **roots)
        with patch.object(self.app, 'comfy_get', return_value={
            'queue_running': [[1, 'stalled-job']], 'queue_pending': [],
        }), patch.object(self.app, 'comfy_post') as comfy_post:
            result = self.app.fail_stalled_v2_task({'taskId': task['id']}, self.user)
        detail = self.app.V2_API.get('/api/v2/tasks/' + task['id'], self.user)[1]
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(detail['status'], 'failed')
        self.assertIn('用户确认任务异常', detail['error'])
        comfy_post.assert_called_once_with('/interrupt', {})


if __name__ == '__main__':
    unittest.main()
