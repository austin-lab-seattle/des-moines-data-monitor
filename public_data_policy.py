"""Server-enforced public window. An API key never grants archive access."""
from datetime import datetime, timedelta, timezone
import json


def now_utc():
    return datetime.now(timezone.utc)


def bounds():
    now = now_utc()
    # Match the dashboard's UTC-hour-aligned 14-day window. Rounding inward
    # keeps Gold buckets exact and never releases a sample older than 14 days.
    end_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return end_hour - timedelta(days=14), now


def restrict(api, event):
    params = dict(api.query_params(event))
    if any(key in params for key in ('include_flagged', 'full_history', 'dataset', 'source_key')):
        return None, api.response(403, {'error': 'Research export options require approved Team Console access.'})
    floor, ceiling = bounds()
    start = api.datetime_for_compare(params.get('start'))
    end = api.datetime_for_compare(params.get('end'))
    try:
        api.validate_time_range(params.get('start'), params.get('end'), start, end)
    except ValueError as exc:
        return None, api.response(400, {'error': str(exc)})
    lower = floor.replace(tzinfo=None)
    upper = ceiling.replace(tzinfo=None)
    if (start is not None and start < lower) or (end is not None and end < lower):
        return None, api.response(403, {
            'error': 'Public data is limited to the latest 14 days. Full history requires approved Team Console access.',
            'allowed_start': floor.isoformat(), 'allowed_end': ceiling.isoformat(),
        })
    if start is not None and start > upper:
        return None, api.response(400, {'error': 'Start must not be in the future.'})
    params['start'] = (start or lower).replace(tzinfo=timezone.utc).isoformat()
    params['end'] = min(end or upper, upper).replace(tzinfo=timezone.utc).isoformat()
    return {**event, 'queryStringParameters': params}, None


def export_response(api, result):
    payload = json.loads(result.get('body') or '{}')
    if 'max_days' in payload:
        payload['max_days'] = 14
        result = {**result, 'body': json.dumps(payload)}
    return result
