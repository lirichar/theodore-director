"""Allowlisted local workflow adapters; clients never choose arbitrary graph paths."""

import json
from pathlib import Path


CHARACTER_MODELS = {
    'diffusion_models': ['minimax_h3_ref2va_pruned_int8_convrot.safetensors'],
    'text_encoders': ['qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'],
    'vae': ['minimax_h3_video_vae_fp16.safetensors', 'minimax_h3_audio_vae_fp32.safetensors'],
    'loras': ['minimax_h3_turbo_v4_step600_ema.safetensors'],
}
QWEN_MODELS = {
    'diffusion_models': ['qwen_image_edit_2511_fp8_e4m3fn.safetensors'],
    'text_encoders': ['qwen_2.5_vl_7b_fp8_scaled.safetensors'],
    'vae': ['qwen_image_vae.safetensors'],
}


def missing_dependencies(graph_path, comfy_root, models):
    missing = [] if Path(graph_path).is_file() else ['工作流文件']
    for category, names in models.items():
        missing.extend(name for name in names if not (Path(comfy_root) / 'models' / category / name).is_file())
    return missing


def workflow_catalog(qwen_path, character_path, comfy_root):
    items = []
    for workflow_id, name, kind, graph, models, description in (
        ('qwen_image_edit', 'Qwen 图片编辑', 'image_edit', qwen_path, QWEN_MODELS,
         '1–3 张图片：指令编辑、修图、风格调整和对象替换'),
        ('character_replace', 'MiniMax 人物替换', 'video', character_path, CHARACTER_MODELS,
         '源视频 + 人物参考图：保留动作、镜头和构图，可保留源音轨'),
    ):
        missing = missing_dependencies(graph, comfy_root, models)
        items.append(dict(id=workflow_id, name=name, kind=kind, description=description,
                          configured=not missing, missing=missing, execution='local_comfyui'))
    return {'workflows': items, 'note': '依赖检查只确认本机文件；节点可用性在提交前检查，不代表已完成 GPU 验收'}


def character_replace_graph(graph_path, *, source_video, character_image, prompt,
                            seed, aspect, resolution, duration, start_time=0,
                            preserve_audio=True):
    try:
        graph = json.loads(Path(graph_path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('人物替换工作流缺失或无法读取，请检查服务端配置') from exc
    required = {
        '92': 'SaveVideo', '115': 'ResolutionSelector', '129': 'RandomNoise',
        '132': 'PrimitiveFloat', '136': 'MiniMaxH3ReferenceToVideo',
        '138': 'PrimitiveStringMultiline', 'replace_character': 'LoadImage',
        'source_video': 'LoadVideo', 'source_slice': 'Video Slice',
        'source_parts': 'GetVideoComponents', '130': 'CreateVideo',
    }
    if not isinstance(graph, dict) or any(not isinstance(graph.get(node), dict) or graph[node].get('class_type') != kind for node, kind in required.items()):
        raise ValueError('人物替换工作流结构不兼容；请使用已验收的 MiniMax v4 API 图')
    graph['source_video']['inputs']['file'] = source_video
    graph['replace_character']['inputs']['image'] = character_image
    graph['138']['inputs']['value'] = prompt
    graph['129']['inputs']['noise_seed'] = seed
    graph['115']['inputs'].update(aspect_ratio=aspect, megapixels=resolution, multiple=32)
    graph['132']['inputs']['value'] = duration
    # Use the same frame grid as the template's ComfyMathExpression.
    frames = max(5, round(duration * 24))
    frames += (5 - frames % 17) % 17
    graph['source_slice']['inputs'].update(start_time=start_time, duration=frames / 24, strict_duration=False)
    # Remove demo refs, including any added to a locally customized template.
    target = graph['136']['inputs']
    for key in list(target):
        if key.startswith(('ref_images.', 'ref_videos.', 'ref_video_audios.', 'ref_audios.')):
            del target[key]
    target.update({'ref_images.ref_image_0': ['replace_character', 0],
                   'ref_videos.ref_video_0': ['source_parts', 0]})
    if preserve_audio:
        target['ref_video_audios.ref_video_audio_0'] = ['source_parts', 1]
        graph['130']['inputs']['audio'] = ['source_parts', 1]
    else:
        graph['130']['inputs'].pop('audio', None)
    # Prune unused nodes so old sample file loaders cannot reach the execution queue.
    reachable = set()
    def visit(node_id):
        if node_id in reachable:
            return
        node = graph.get(node_id)
        if not isinstance(node, dict) or not isinstance(node.get('inputs'), dict):
            raise ValueError('人物替换工作流含失效的节点引用')
        reachable.add(node_id)
        for value in node['inputs'].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
                visit(value[0])
    visit('92')
    return {key: value for key, value in graph.items() if key in reachable}
