"""Synthetic node graphs for adapter tests. These are not executable workflows."""
import json


def _node(kind, **inputs):
    return {'class_type': kind, 'inputs': inputs}


def write_character_graph(path):
    graph = {
        '92': _node('SaveVideo', video=['130', 0]),
        '130': _node('CreateVideo', images=['136', 0]),
        '115': _node('ResolutionSelector'),
        '129': _node('RandomNoise'),
        '132': _node('PrimitiveFloat'),
        '136': _node('MiniMaxH3ReferenceToVideo', prompt=['138', 0],
                     size=['115', 0], noise=['129', 0], seconds=['132', 0]),
        '138': _node('PrimitiveStringMultiline'),
        'replace_character': _node('LoadImage'),
        'source_video': _node('LoadVideo'),
        'source_slice': _node('Video Slice', video=['source_video', 0]),
        'source_parts': _node('GetVideoComponents', video=['source_slice', 0]),
    }
    path.write_text(json.dumps(graph), encoding='utf-8')
    return path


def write_qwen_graph(path):
    graph = {key: _node('Fixture') for key in
             ('195', '170:161', '170:162', '170:151', '170:149', '170:156', '170:169')}
    graph['170:160'] = _node('LoadImage', image='lily_girl_blue.png')
    path.write_text(json.dumps(graph), encoding='utf-8')
    return path
