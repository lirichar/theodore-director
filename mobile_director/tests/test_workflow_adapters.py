import json
import tempfile
import unittest
from pathlib import Path

from director_v2.generation_contract import normalize_request
from director_v2.workflows import character_replace_graph, workflow_catalog
from fixture_graphs import write_character_graph


class WorkflowAdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.template = write_character_graph(Path(temp.name) / 'character.json')

    def test_character_graph_uses_selected_inputs_and_zero_seed(self):
        original = self.template.read_bytes()
        graph = character_replace_graph(self.template, source_video='user/source.mp4', character_image='user/character.png',
            prompt='只替换左侧主角', seed=0, aspect='9:16 (Portrait Widescreen)', resolution=.4, duration=5,
            start_time=2, preserve_audio=True)
        self.assertEqual(graph['source_video']['inputs']['file'], 'user/source.mp4')
        self.assertEqual(graph['replace_character']['inputs']['image'], 'user/character.png')
        self.assertEqual(graph['138']['inputs']['value'], '只替换左侧主角')
        self.assertEqual(graph['129']['inputs']['noise_seed'], 0)
        self.assertEqual(graph['115']['inputs']['aspect_ratio'], '9:16 (Portrait Widescreen)')
        self.assertEqual(graph['source_slice']['inputs']['start_time'], 2)
        self.assertAlmostEqual(graph['source_slice']['inputs']['duration'], 124 / 24)
        self.assertEqual(graph['130']['inputs']['audio'], ['source_parts', 1])
        self.assertNotIn('新堂彩音', json.dumps(graph, ensure_ascii=False))
        self.assertEqual(self.template.read_bytes(), original)

    def test_audio_off_and_demo_nodes_are_removed(self):
        graph = json.loads(self.template.read_text(encoding='utf-8'))
        graph['136']['inputs']['ref_images.ref_image_7'] = ['demo', 0]
        graph['demo'] = {'class_type': 'LoadImage', 'inputs': {'image': 'private_demo.png'}}
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'graph.json'
            file.write_text(json.dumps(graph), encoding='utf-8')
            result = character_replace_graph(file, source_video='a.mp4', character_image='b.png', prompt='替换主角',
                seed=3, aspect='16:9 (Widescreen)', resolution=.4, duration=5, preserve_audio=False)
        self.assertNotIn('audio', result['130']['inputs'])
        self.assertNotIn('ref_video_audios.ref_video_audio_0', result['136']['inputs'])
        self.assertNotIn('demo', result)
        self.assertNotIn('ref_images.ref_image_7', result['136']['inputs'])

    def test_registry_reports_missing_without_private_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            listing = workflow_catalog(root / 'missing_qwen.json', root / 'missing_character.json', root)
            self.assertEqual({item['id'] for item in listing['workflows']}, {'qwen_image_edit', 'character_replace'})
            self.assertTrue(all(not item['configured'] for item in listing['workflows']))
            self.assertNotIn(str(root), json.dumps(listing))

    def test_character_request_keeps_explicit_roles_and_audio_choice(self):
        request, parameters = normalize_request(dict(kind='video', workflowId='character_replace', prompt='替换主角',
            sourceVideoId='video', characterMediaId='character', mediaIds=[], seed=0, startTime=1.2,
            preserveAudio=False, count=2))
        self.assertEqual(parameters['workflowId'], 'character_replace')
        self.assertEqual(request['mode'], 'r2v')
        self.assertEqual(parameters['mediaIds'], ['video', 'character'])
        self.assertEqual(parameters['startTime'], 1.2)
        self.assertFalse(parameters['preserveAudio'])

    def test_unknown_workflow_and_invalid_roles_are_rejected(self):
        base = dict(kind='video', prompt='替换主角', workflowId='character_replace', sourceVideoId='a', characterMediaId='b')
        for change in [dict(workflowId='../arbitrary.json'), dict(kind='image'), dict(sourceVideoId=''),
                       dict(characterMediaId='a'), dict(mediaIds=['c']), dict(preserveAudio='false'), dict(startTime=-1)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_request(dict(base, **change))


if __name__ == '__main__':
    unittest.main()
