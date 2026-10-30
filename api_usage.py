"""Durable named-researcher usage. Public anonymous traffic is not attributed.

Counters and daily history have no TTL. Only request deduplication markers
expire. Counts start when this tracker first observes the user, never at a
fabricated historical zero. Export totals measure files prepared, not S3 GETs.
"""
import hashlib
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DAY_TIMEZONE = ZoneInfo('America/Los_Angeles')
METRICS = ('requests', 'successful_requests', 'failed_requests', 'exports_prepared', 'export_bytes', 'export_rows')


def subject_id(email, key_id):
    identity = str(email or '').strip().lower() or f'key:{key_id}'
    return hashlib.sha256(identity.encode()).hexdigest()


def item_number(item, name, default=0):
    return int(item.get(name, {}).get('N', default))


def read_record(client, table, counter_id):
    return client.get_item(TableName=table, Key={'counter_id': {'S': counter_id}}, ConsistentRead=True).get('Item', {})


def snapshot(item):
    if not item:
        return None
    return {**{name: item_number(item, name) for name in METRICS},
            'tracking_since': item.get('tracking_since', {}).get('S'),
            'last_used_at': item.get('last_used_at', {}).get('S')}


def record_usage(client, table, identity, request_id, status, export=None, at=None):
    """Commit four rollups and a deduplication marker in one transaction.

    Optimistic versions preserve the maximum last-used timestamp even when
    requests finish out of order. Retrying the same gateway request cannot
    increment counters twice within the seven-day marker retention window.
    """
    at = at or datetime.now(timezone.utc)
    stamp = at.isoformat()
    epoch = int(at.timestamp() * 1_000_000)
    key_id = identity['key_id']
    user_id = subject_id(identity.get('email'), key_id)
    day = at.astimezone(DAY_TIMEZONE).date().isoformat()
    marker = 'usage:event:' + hashlib.sha256(request_id.encode()).hexdigest()
    generated = export if export and 200 <= status < 300 else None
    numbers = dict(requests=1, successful_requests=int(200 <= status < 300),
                   failed_requests=int(not 200 <= status < 300), exports_prepared=int(bool(generated)),
                   export_bytes=int(generated['bytes']) if generated else 0,
                   export_rows=int(generated['rows']) if generated else 0)
    for attempt in range(5):
        if read_record(client, table, marker):
            return
        updates = []
        for kind, identifier in [('user', user_id), ('key', key_id)]:
            lifetime_id = f'usage:{kind}:{identifier}:lifetime'
            previous = read_record(client, table, lifetime_id)
            version = item_number(previous, 'version')
            latest_epoch = max(epoch, item_number(previous, 'last_used_epoch'))
            latest_stamp = stamp if latest_epoch == epoch else previous['last_used_at']['S']
            first_epoch = min(epoch, item_number(previous, 'tracking_since_epoch', epoch))
            first_stamp = stamp if first_epoch == epoch else previous['tracking_since']['S']
            values = {':stamp': {'S': first_stamp}, ':first_epoch': {'N': str(first_epoch)}, ':last': {'S': latest_stamp}, ':epoch': {'N': str(latest_epoch)},
                      ':version': {'N': str(version + 1)}, ':prior': {'N': str(version)}}
            values.update({f':m{i}': {'N': str(numbers[name])} for i, name in enumerate(METRICS)})
            names = {f'#m{i}': name for i, name in enumerate(METRICS)}
            names['#version'] = 'version'
            additions = ', '.join(f'#m{i} :m{i}' for i in range(len(METRICS)))
            condition = '#version = :prior' if previous else 'attribute_not_exists(#version)'
            if not previous:
                values.pop(':prior')
            updates.append({'Update': {'TableName': table, 'Key': {'counter_id': {'S': lifetime_id}},
                'UpdateExpression': 'SET tracking_since = :stamp, tracking_since_epoch = :first_epoch, last_used_at = :last, last_used_epoch = :epoch, #version = :version ADD ' + additions,
                'ConditionExpression': condition, 'ExpressionAttributeNames': names, 'ExpressionAttributeValues': values}})
            day_values = {f':m{i}': {'N': str(numbers[name])} for i, name in enumerate(METRICS)}
            day_values.update({':day': {'S': day}, ':zone': {'S': str(DAY_TIMEZONE)}})
            updates.append({'Update': {'TableName': table,
                'Key': {'counter_id': {'S': f'usage:{kind}:{identifier}:day:{day}'}},
                'UpdateExpression': 'SET usage_day = :day, day_timezone = :zone ADD ' + additions,
                'ExpressionAttributeNames': {f'#m{i}': name for i, name in enumerate(METRICS)},
                'ExpressionAttributeValues': day_values}})
        updates.append({'Put': {'TableName': table, 'Item': {'counter_id': {'S': marker},
            'ttl': {'N': str(int(at.timestamp()) + 7 * 86400)}}, 'ConditionExpression': 'attribute_not_exists(counter_id)'}})
        try:
            client.transact_write_items(TransactItems=updates)
            return
        except Exception as exc:
            code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
            if code not in {'TransactionCanceledException', 'TransactionConflictException'} or attempt == 4:
                raise
            time.sleep(.02 * 2 ** attempt)


def read_usage(client, table, kind, identifier, day):
    return {'lifetime': snapshot(read_record(client, table, f'usage:{kind}:{identifier}:lifetime')),
            'today': snapshot(read_record(client, table, f'usage:{kind}:{identifier}:day:{day}'))}


def read_history(client, table, user_id, days, today, tracking_since):
    first_day = datetime.fromisoformat(tracking_since).astimezone(DAY_TIMEZONE).date() if tracking_since else None
    rows = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        value = snapshot(read_record(client, table, f'usage:user:{user_id}:day:{day.isoformat()}'))
        covered = first_day is not None and day >= first_day
        rows.append({'day': day.isoformat(), 'tracked': covered, 'partial_day': day == first_day,
                     **{name: value[name] if value else 0 if covered else None for name in METRICS}})
    return rows
