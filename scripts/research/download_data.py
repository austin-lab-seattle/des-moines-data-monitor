"""Download an approved research export with team sign-in; no public API key.

Uses the existing public OAuth client with PKCE and its registered loopback
callback. Tokens stay in memory. Run this helper yourself to complete MFA.
"""
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser

CLIENT = '5tno54tskp2tk6lhh6c8jh9ev4'
AUTHORITY = 'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_oR7CIzxQG'
HOSTED = 'https://des-moines-air-team-213598695875.auth.us-west-2.amazoncognito.com'
CALLBACK = 'http://127.0.0.1:5173/team'
BASE = 'https://yvhb48sthk.execute-api.us-west-2.amazonaws.com/air-quality/internal/v1/research'


def url64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def sign_in():
    verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            valid = parsed.path == '/team' and secrets.compare_digest(query.get('state', [''])[0], state)
            if valid:
                result['code'] = query.get('code', [''])[0]
                result['error'] = bool(query.get('error'))
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b'Sign-in received. Return to your terminal.' if valid else b'Invalid sign-in callback.')

        def log_message(self, *args):
            pass  # Authorization codes must not appear in local access logs.

    try:
        server = HTTPServer(('127.0.0.1', 5173), Callback)
    except OSError as exc:
        raise RuntimeError('Port 5173 is in use. Stop the local development server or use Research data in the Team Console.') from exc
    with server:
        server.timeout = 1
        query = urllib.parse.urlencode({'response_type': 'code', 'client_id': CLIENT, 'redirect_uri': CALLBACK,
            'scope': 'openid email profile', 'state': state, 'nonce': nonce,
            'code_challenge_method': 'S256', 'code_challenge': url64(hashlib.sha256(verifier.encode()).digest())})
        print('Complete team sign-in and MFA in your browser. Tokens will not be printed or saved.', flush=True)
        webbrowser.open(f'{HOSTED}/oauth2/authorize?{query}')
        deadline = time.monotonic() + 300
        while 'code' not in result and time.monotonic() < deadline:
            server.handle_request()
        if not result.get('code') or result.get('error'):
            raise RuntimeError('Team sign-in did not complete.')
    body = urllib.parse.urlencode({'grant_type': 'authorization_code', 'client_id': CLIENT,
        'redirect_uri': CALLBACK, 'code': result['code'], 'code_verifier': verifier}).encode()
    with urllib.request.urlopen(urllib.request.Request(f'{HOSTED}/oauth2/token', data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}), timeout=30) as response:
        token = json.load(response)['id_token']
    # The TLS token exchange and API Gateway provide token authenticity. The
    # nonce ties this response to this particular browser login.
    encoded = token.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
    if claims.get('nonce') != nonce or claims.get('iss') != AUTHORITY or claims.get('aud') != CLIENT:
        raise RuntimeError('The sign-in response does not match this request.')
    return token


def request(token, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def save_download(result, destination):
    url = urllib.parse.urlsplit(result['url'])
    if url.scheme != 'https' or not url.hostname.endswith('.amazonaws.com'):
        raise ValueError('Unexpected download host')
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            # Do not forward the team bearer token to S3.
            with urllib.request.urlopen(result['url'], timeout=60) as response:
                while block := response.read(1024 * 1024):
                    output.write(block); digest.update(block)
        if digest.hexdigest() != result['sha256']:
            raise RuntimeError('The downloaded file failed its integrity check.')
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instrument', required=True, choices=['BC-MA200', 'CO2-LICOR', 'NEPH-PM25', 'NO2-CAPS', 'SMPS'])
    parser.add_argument('--include-flagged', action='store_true')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    destination = args.output or Path(f'{args.instrument}_research.csv.gz')
    if destination.exists() and not args.overwrite:
        parser.error('Output already exists. Choose another filename or explicitly use --overwrite.')
    token = os.environ.get('AQ_TEAM_ID_TOKEN') or sign_in()
    body = {'instrument': args.instrument, 'include_flagged': args.include_flagged}
    if args.start:
        body['start'] = args.start
    if args.end:
        body['end'] = args.end
    job = request(token, '/exports', body)
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        status = request(token, f'/exports/{job["job_id"]}')
        if status['state'] == 'READY':
            result = request(token, f'/exports/{job["job_id"]}/download')
            if result.get('state') != 'READY' or not result.get('url'):
                raise RuntimeError('The research snapshot changed. Prepare a new export.')
            save_download(result, destination)
            print(f'Saved {result["rows"]:,} rows to {destination}.')
            return
        if status['state'] not in {'PENDING', 'PROCESSING'}:
            raise RuntimeError(f'Export did not complete: {status["state"]}')
        time.sleep(3)
    raise TimeoutError('Export is still preparing. Check Your exports in the Team Console.')


if __name__ == '__main__':
    main()
