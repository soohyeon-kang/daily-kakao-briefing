"""One-time setup; credentials stay in memory and GitHub Secrets, never in chat."""
import base64
import argparse
from getpass import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import time
import urllib.parse
import webbrowser

from nacl.public import PublicKey, SealedBox
from nacl.secret import SecretBox

from briefing import ROOT, encrypt_token, json_request

REDIRECT = 'http://localhost:8765/callback'


def github(token, method, path, data=None):
    return json_request('https://api.github.com' + path, method=method, data=data,
                        headers={'Authorization': 'Bearer ' + token,
                                 'Accept': 'application/vnd.github+json',
                                 'X-GitHub-Api-Version': '2022-11-28'})


def put_secret(token, repo, name, value, public_key):
    encrypted = SealedBox(PublicKey(base64.b64decode(public_key['key']))).encrypt(value.encode())
    github(token, 'PUT', f'/repos/{repo}/actions/secrets/{name}',
           {'encrypted_value': base64.b64encode(encrypted).decode(), 'key_id': public_key['key_id']})


def authorize(client_id, client_secret):
    state = secrets.token_urlsafe(32)
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Authorization codes must not enter terminal or access logs.

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != '/callback' or query.get('state') != [state]:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid callback. Return to the setup terminal.')
                return
            received.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write('인증을 받았습니다. 설치 창으로 돌아가세요.'.encode())

    url = 'https://kauth.kakao.com/oauth/authorize?' + urllib.parse.urlencode({
        'response_type': 'code', 'client_id': client_id, 'redirect_uri': REDIRECT,
        'scope': 'talk_message', 'state': state})
    with HTTPServer(('127.0.0.1', 8765), Callback) as server:
        server.timeout = 1
        print('브라우저에서 본인 카카오 로그인 및 메시지 전송 동의를 완료하세요.')
        print('브라우저가 열리지 않으면 다음 주소를 직접 여세요:\n' + url)
        webbrowser.open(url)
        deadline = time.monotonic() + 600
        while not received and time.monotonic() < deadline:
            server.handle_request()
    if not received.get('code') or 'error' in received:
        raise RuntimeError('Kakao authorization was not completed within 10 minutes')
    data = {'grant_type': 'authorization_code', 'client_id': client_id,
            'redirect_uri': REDIRECT, 'code': received['code']}
    if client_secret:
        data['client_secret'] = client_secret
    tokens = json_request('https://kauth.kakao.com/oauth/token', data=data, form=True)
    if not tokens.get('refresh_token') or 'talk_message' not in tokens.get('scope', '').split():
        raise RuntimeError('Kakao talk_message permission or refresh token missing')
    return tokens['refresh_token']


def upload(token, repo, files, parent):
    tree = github(token, 'POST', f'/repos/{repo}/git/trees', {'tree': files})
    commit = github(token, 'POST', f'/repos/{repo}/git/commits',
                    {'message': 'Install free daily economic and tax briefing',
                     'tree': tree['sha'], 'parents': [parent]})
    github(token, 'PATCH', f'/repos/{repo}/git/refs/heads/main', {'sha': commit['sha']})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reauthorize', action='store_true')
    args = parser.parse_args()
    print('무료 클라우드 브리핑 설치 · 마우스 제어 없음')
    print('공개 저장소에 공개 뉴스와 차트를 게시합니다. 계좌·보유 수량은 포함하지 않습니다.')
    repo = input('전용 GitHub 저장소 owner/repo: ').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('owner/repo 형식이 필요합니다')
    token = getpass('이 저장소 전용 GitHub 토큰(화면에 표시 안 됨): ').strip()
    metadata = github(token, 'GET', f'/repos/{repo}')
    if metadata.get('private') or metadata.get('default_branch') != 'main':
        raise RuntimeError('공개 저장소 + main 기본 브랜치가 필요합니다')
    contents = github(token, 'GET', f'/repos/{repo}/contents/')
    if not args.reauthorize and any(item['name'] != 'README.md' for item in contents):
        raise RuntimeError('전용 새 저장소만 사용하세요. 기존 프로젝트를 덮어쓰지 않습니다.')
    if args.reauthorize and not any(item['name'] == 'briefing.py' for item in contents):
        raise RuntimeError('이 브리핑이 설치된 저장소가 아닙니다')
    if input('공개 뉴스 게시에 동의하면 PUBLIC 입력: ').strip() != 'PUBLIC':
        raise RuntimeError('설치 중지: 아무것도 업로드하지 않았습니다')
    client_id = getpass('카카오 REST API 키: ').strip()
    client_secret = getpass('카카오 Client Secret(ON 유지): ').strip()
    if not client_id or not client_secret:
        raise ValueError('카카오 REST API 키와 Client Secret이 필요합니다')
    refresh = authorize(client_id, client_secret)
    key = base64.b64encode(secrets.token_bytes(SecretBox.KEY_SIZE)).decode()
    public_key = github(token, 'GET', f'/repos/{repo}/actions/secrets/public-key')
    for name, value in [('KAKAO_REST_API_KEY', client_id), ('KAKAO_CLIENT_SECRET', client_secret), ('TOKEN_KEY', key)]:
        if value:
            put_secret(token, repo, name, value, public_key)
    if args.reauthorize:
        old = github(token, 'GET', f'/repos/{repo}/contents/.state/kakao.enc')
        github(token, 'PUT', f'/repos/{repo}/contents/.state/kakao.enc', {
            'message': 'Renew encrypted Kakao authorization', 'sha': old['sha'],
            'content': base64.b64encode(encrypt_token(refresh, key)).decode(), 'branch': 'main'})
    else:
        files = []
        included = ['briefing.py', 'setup.py', 'install.cmd', 'test_briefing.py', 'requirements.txt', 'README.md', '.gitignore',
                    '.github/workflows/briefing.yml']
        for name in included:
            files.append({'path': name, 'mode': '100644', 'type': 'blob',
                          'content': (ROOT / name).read_text(encoding='utf-8')})
        files += [{'path': '.state/kakao.enc', 'mode': '100644', 'type': 'blob',
                   'content': encrypt_token(refresh, key).decode()},
                  {'path': 'docs/.gitkeep', 'mode': '100644', 'type': 'blob', 'content': ''}]
        parent = github(token, 'GET', f'/repos/{repo}/git/ref/heads/main')['object']['sha']
        upload(token, repo, files, parent)
    print('업로드 완료. 테스트 실행을 요청합니다(성공 응답 전에는 발송 완료가 아닙니다).')
    # Registration can lag just after the initial workflow commit.
    for attempt in range(5):
        try:
            github(token, 'POST', f'/repos/{repo}/actions/workflows/briefing.yml/dispatches',
                   {'ref': 'main', 'inputs': {'send': 'true'}})
            break
        except RuntimeError as exc:
            if 'HTTP 404' not in str(exc) or attempt == 4:
                raise
            time.sleep(3)
    print(f'실행 확인: https://github.com/{repo}/actions')
    print('작업 마지막에 Kakao API result_code=0을 확인하고 휴대폰에서 수신을 확인하세요.')
    print('이후 PC를 꺼도 GitHub에서 한국 공휴일을 제외한 평일 오전 9시(KST, 지연 가능)에 실행됩니다.')
    print('GitHub 토큰은 저장하지 않았습니다. 최초 성공 후 해당 토큰을 폐기해도 됩니다.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'설치 중지: {type(exc).__name__}: {exc}')
        raise SystemExit(1)
