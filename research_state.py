"""Small private job records with S3 conditional-write concurrency control."""
import hashlib
import json
import time

from botocore.exceptions import ClientError


class Conflict(Exception):
    pass


class RateExceeded(Exception):
    pass


class ResearchState:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    @staticmethod
    def key(identity):
        return 'state/' + hashlib.sha256(identity.encode()).hexdigest() + '.json'

    def read(self, identity):
        try:
            result = self.client().get_object(Bucket=self.bucket(), Key=self.key(identity))
        except ClientError as error:
            if error.response['Error']['Code'] in {'NoSuchKey', '404', 'NotFound'}:
                return None, None
            raise
        stream = result['Body']
        try:
            raw = stream.read(256 * 1024 + 1)
        finally:
            stream.close()
        if len(raw) > 256 * 1024:
            raise ValueError('Oversized job state')
        item = json.loads(raw)
        if item.get('job_id') != identity:
            raise ValueError('Job state identity mismatch')
        return item, result['ETag']

    def get(self, identity):
        return self.read(identity)[0]

    def write(self, item, etag=None):
        condition = {'IfMatch': etag} if etag else {'IfNoneMatch': '*'}
        try:
            self.client().put_object(Bucket=self.bucket(), Key=self.key(item['job_id']),
                Body=json.dumps(item, sort_keys=True).encode(), ContentType='application/json',
                CacheControl='no-store', **condition)
        except ClientError as error:
            if error.response['Error']['Code'] in {'PreconditionFailed', 'ConditionalRequestConflict', '412', '409'}:
                raise Conflict() from error
            raise

    def update(self, identity, changes, expected_state=None):
        for _ in range(5):
            item, etag = self.read(identity)
            if not item or expected_state and item.get('state') != expected_state:
                raise Conflict()
            try:
                self.write({**item, **changes}, etag)
                return
            except Conflict:
                continue
        raise Conflict()

    def rate(self, subject):
        now = int(time.time())
        identity = f'rate#{subject}#{now // 3600}'
        for _ in range(5):
            item, etag = self.read(identity)
            count = item.get('requests', 0) if item else 0
            if count >= 10:
                raise RateExceeded()
            try:
                self.write({'job_id': identity, 'requests': count + 1, 'expires_at': now + 7200}, etag)
                return
            except Conflict:
                continue
        raise Conflict()

    def add_job(self, job):
        self.write(job)
        identity = f'history#{job["owner"]}'
        for _ in range(5):
            history, etag = self.read(identity)
            entries = (history or {}).get('entries', [])
            entries = [entry for entry in entries if entry['job_id'] != job['job_id']]
            entries.append({'job_id': job['job_id'], 'created_at': job['created_at']})
            entries.sort(key=lambda entry: entry['created_at'], reverse=True)
            try:
                self.write({'job_id': identity, 'entries': entries[:20], 'expires_at': int(time.time()) + 86400}, etag)
                return
            except Conflict:
                continue
        raise Conflict()

    def history(self, subject):
        history = self.get(f'history#{subject}') or {}
        results = []
        for entry in history.get('entries', [])[:20]:
            job = self.get(entry['job_id'])
            if job and job.get('kind') == 'request' and job.get('owner') == subject:
                results.append(job)
        return results
