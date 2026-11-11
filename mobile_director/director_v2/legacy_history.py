"""Read-only compatibility view; never invent status, timestamps or version IDs."""
import hashlib


def legacy_history(records, user, query='', offset=0, limit=30):
    fields = ('mode', 'aspect', 'duration', 'resolution', 'count', 'negative', 'editMode', 'silent')
    items = []
    for prefix, value in reversed(list(records.items())):
        if not isinstance(value, dict) or value.get('v2_task_id'):
            continue
        # Old ownerless records belong to the original local administrator only.
        owner = value.get('user_id')
        if owner != user['id'] and not (owner is None and user['role'] == 'admin'):
            continue
        title = str(value.get('title') or '旧版生成记录')
        prompt = str(value.get('prompt') or '')
        if query.casefold() not in (title + ' ' + prompt).casefold():
            continue
        items.append({'id': 'legacy_' + hashlib.sha256(str(prefix).encode()).hexdigest()[:24],
                      'title': title, 'prompt': prompt, 'kind': str(value.get('kind') or 'video'),
                      'status': 'unknown', 'parameters': {k: value[k] for k in fields if k in value},
                      'source': 'legacy', 'createdAt': None})
    return {'items': items[offset:offset + limit], 'total': len(items),
            'offset': offset, 'hasMore': offset + limit < len(items)}
