"""Validate only the controls actually implemented by the director adapters."""

import math
import secrets


ASPECTS = {
    "1:1": "1:1 (Square)", "2:3": "2:3 (Portrait Photo)",
    "3:2": "3:2 (Photo)", "3:4": "3:4 (Portrait Standard)",
    "4:3": "4:3 (Standard)", "9:16": "9:16 (Portrait Widescreen)",
    "16:9": "16:9 (Widescreen)", "21:9": "21:9 (Ultrawide)",
}


def normalize_request(request, image_size="1536x1024"):
    if not isinstance(request, dict):
        raise ValueError("生成请求必须是对象")
    req = dict(request)
    kind = req.get("kind") or "video"
    if kind not in {"image", "image_edit", "video"}:
        raise ValueError("不支持的生成类型")
    workflow_id = req.get('workflowId') or ('qwen_image_edit' if kind == 'image_edit' else '')
    if workflow_id not in {'', 'qwen_image_edit', 'character_replace'}:
        raise ValueError('不支持的工作流')
    if (workflow_id == 'qwen_image_edit' and kind != 'image_edit') or (workflow_id == 'character_replace' and kind != 'video'):
        raise ValueError('工作流与生成类型不匹配')
    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
        raise ValueError("请输入 1 至 20000 字的提示词")
    count = req.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 8:
        raise ValueError("生成数量必须是 1 至 8 的整数")
    media_ids = req.get("mediaIds") or []
    if not isinstance(media_ids, list) or any(not isinstance(x, str) or not x for x in media_ids):
        raise ValueError("参考素材必须是素材 ID 列表")
    media_ids = list(dict.fromkeys(media_ids))
    if workflow_id == 'character_replace':
        source_id, character_id = req.get('sourceVideoId'), req.get('characterMediaId')
        if not isinstance(source_id, str) or not source_id or not isinstance(character_id, str) or not character_id or source_id == character_id:
            raise ValueError('人物替换需要分别选择源视频和人物参考图')
        if any(value not in {source_id, character_id} for value in media_ids):
            raise ValueError('人物替换只接受指定的源视频和人物参考图')
        media_ids = [source_id, character_id]
        req['mode'] = 'r2v'
    req.update(kind=kind, prompt=prompt.strip(), count=count, mediaIds=media_ids)
    parameters = {}
    if workflow_id:
        parameters['workflowId'] = workflow_id
    if workflow_id == 'character_replace':
        start = req.get('startTime', 0)
        audio = req.get('preserveAudio', True)
        if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or not 0 <= start <= 86400:
            raise ValueError('源视频起点必须为 0–86400 秒')
        if not isinstance(audio, bool):
            raise ValueError('保留源音轨必须是布尔值')
        parameters.update(sourceVideoId=source_id, characterMediaId=character_id, startTime=start, preserveAudio=audio)
    if kind == "image":
        if media_ids or req.get("firstFrameId") or req.get("lastFrameId"):
            raise ValueError("当前图片生成接口不支持参考图，请切换到图片编辑")
        size = req.get("size") or image_size
        if size not in {"1024x1024", "1536x1024", "1024x1536", "auto"}:
            raise ValueError("不支持的图片尺寸")
        parameters["size"] = size
    else:
        seed = req.get("seed")
        if seed is None or seed == "":
            seed = secrets.randbelow(2**50)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**53 - 9:
            raise ValueError("种子必须是安全范围内的非负整数")
        parameters["seed"] = seed
        if kind == "image_edit":
            if not 1 <= len(media_ids) <= 3:
                raise ValueError("图片编辑需要 1 至 3 张参考图片")
            negative = req.get("negative", "")
            if not isinstance(negative, str) or len(negative) > 20000:
                raise ValueError("反向提示词格式不正确")
            parameters["negative"] = negative
        else:
            mode = req.get("mode") or "t2v"
            if mode not in {"t2v", "i2v", "r2v"}:
                raise ValueError("视频生成方式不正确")
            aspect = req.get("aspect") or "16:9"
            aspect = ASPECTS.get(aspect, aspect)
            if aspect not in ASPECTS.values():
                raise ValueError("画幅不正确")
            parameters.update(mode=mode, aspect=aspect)
            for key, default, low, high in (("duration", 5, 5, 15), ("resolution", .4, .1, 1.0)):
                value = req.get(key, default)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f"{key} 必须介于 {low} 和 {high} 之间")
                parameters[key] = value
            if mode == "t2v" and (media_ids or req.get("firstFrameId") or req.get("lastFrameId")):
                raise ValueError("纯文本视频不使用参考素材，请切换到参考生成或首尾帧")
            if mode == "r2v" and not 1 <= len(media_ids) <= 12:
                raise ValueError("参考视频生成需要参考图片或视频")
            if mode == "i2v":
                if not isinstance(req.get("firstFrameId"), str) or not req["firstFrameId"]:
                    raise ValueError("请选择首帧图片")
                for key in ("firstFrameId", "lastFrameId"):
                    value = req.get(key) or ""
                    if not isinstance(value, str):
                        raise ValueError("首尾帧必须是素材 ID")
                    parameters[key] = value
                    if value and value not in media_ids:
                        media_ids.append(value)
            shots = req.get("shots") or []
            if not isinstance(shots, list) or len(shots) > 100:
                raise ValueError("多镜头参数格式不正确")
            for shot in shots:
                if not isinstance(shot, dict) or not isinstance(shot.get("text"), str):
                    raise ValueError("多镜头提示词格式不正确")
                seconds = shot.get("seconds", 2)
                if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not .5 <= seconds <= 15:
                    raise ValueError("单镜头时长格式不正确")
            parameters["shots"] = [{"text": s["text"], "seconds": s.get("seconds", 2)} for s in shots]
    parameters["mediaIds"] = media_ids
    for key in ("parentVersionId", "entityId", "entityType"):
        if req.get(key):
            if not isinstance(req[key], str):
                raise ValueError("生成关联 ID 格式不正确")
            parameters[key] = req[key]
    # Do not forward silently ignored UI controls to the adapters or task history.
    for key in ("strength", "steps", "guidance", "sampler", "scheduler", "negative", "seed", "size", "shots", "mode", "aspect", "resolution", "duration", "firstFrameId", "lastFrameId"):
        req.pop(key, None)
    req.update(parameters)
    return req, parameters
