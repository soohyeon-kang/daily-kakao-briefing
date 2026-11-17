"""Free, headline-based briefing. No paid AI API and no desktop automation."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import holidays
from nacl.secret import SecretBox
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
MARKETS = [('S&P 500', '^GSPC', 'pt'), ('나스닥100', '^NDX', 'pt'),
           ('원/달러', 'KRW=X', '원'), ('미국 10년 금리', '^TNX', '%'),
           ('VIX', '^VIX', 'pt'), ('금 선물', 'GC=F', 'USD/oz')]
TAX_DOMAINS = ('nts.go.kr', 'moef.go.kr', 'mofe.go.kr', 'molit.go.kr',
               'law.go.kr', 'tt.go.kr', 'korea.kr')
MARKET_DOMAINS = ('yna.co.kr', 'reuters.com', 'news.einfomax.co.kr', 'hankyung.com', 'sedaily.com',
                  'mk.co.kr', 'kbthink.com', 'ytn.co.kr', 'newsis.com', 'news1.kr', 'chosun.com',
                  'khan.co.kr', 'hani.co.kr', 'joongang.co.kr', 'fnnews.com', 'mt.co.kr', 'asiae.co.kr',
                  'imbc.com', 'sbs.co.kr', 'kbs.co.kr')


def request(url, *, data=None, headers=None, form=False, method=None):
    headers = {'User-Agent': 'DailyBriefing/1.0', **(headers or {})}
    if data is not None:
        data = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
        headers['Content-Type'] = ('application/x-www-form-urlencoded' if form
                                   else 'application/json')
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            body = response.read(8_000_001)
            if len(body) > 8_000_000:
                raise RuntimeError('Response exceeds size limit')
            return body
    except urllib.error.HTTPError as exc:
        # Never print response bodies or credential-bearing URLs.
        raise RuntimeError(f'HTTP {exc.code} from {urllib.parse.urlsplit(url).hostname}') from None


def json_request(url, **kwargs):
    body = request(url, **kwargs)
    return json.loads(body) if body.strip() else {}


def is_tax_source(url):
    host = (urllib.parse.urlsplit(url).hostname or '').lower()
    return any(host == domain or host.endswith('.' + domain) for domain in TAX_DOMAINS)


def parse_news(xml, group, now):
    result, seen = [], set()
    for item in ET.fromstring(xml).findall('./channel/item'):
        title = html.unescape(item.findtext('title', '')).strip()
        link = item.findtext('link', '')
        source = item.find('source')
        source_url = source.get('url', '') if source is not None else ''
        source_name = (source.text or '') if source is not None else ''
        if not source_url:
            # Bing RSS wraps original URLs and uses a namespaced Source element.
            original = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get('url', [link])[0]
            if (urllib.parse.urlsplit(link).hostname or '').endswith('bing.com'):
                link, source_url = original, original
                source_name = next((e.text or '' for e in item if e.tag.rsplit('}', 1)[-1].lower() == 'source'), '')
        suffix = ' - ' + source_name
        if source_name and title.endswith(suffix):
            title = title[:-len(suffix)].rstrip()
        if len(title) < 8 or re.search(r'undefined|\bnull\b', title, re.IGNORECASE) or title.count('>') >= 2:
            continue
        try:
            published = parsedate_to_datetime(item.findtext('pubDate', ''))
            if published.tzinfo is None:
                continue
        except (ValueError, TypeError):
            continue
        max_age = 7 if group == '부동산·세무' else 2
        if not now - timedelta(days=max_age) <= published <= now + timedelta(minutes=5):
            continue
        if group == '부동산·세무' and not is_tax_source(source_url):
            continue
        source_host = (urllib.parse.urlsplit(source_url).hostname or '').lower()
        if group == '미국주식·경제' and not any(source_host == d or source_host.endswith('.' + d) for d in MARKET_DOMAINS):
            continue
        if not title or urllib.parse.urlsplit(link).scheme != 'https' or title in seen:
            continue
        if any(word in title for word in ('산업의 날', '장관동정', '기념식', '표창', '인사발령', '사회공헌', '채용', '공무원')):
            continue
        if source_name == '국가법령정보센터' and not any(word in title for word in ('개정', '판례', '해석', '예규')):
            continue
        if group == '미국주식·경제' and any(word in title for word in ('코스피', '코스닥', '비트코인')):
            continue
        seen.add(title)
        result.append({'group': group, 'text': title, 'url': link,
                       'source': source_name,
                       'published_at': published.isoformat(),
                       'published': published.astimezone(KST).strftime('%m/%d %H:%M')})
    if group == '부동산·세무':
        tax_terms = ('세제', '세법', '양도', '부가세', '소득세', '상속', '증여', '취득세', '종부세',
                     '납세', '국세', '예규', '판례', '시행령', '신고', '주택공급', '대출', '규제지역')
        result.sort(key=lambda item: sum(term in item['text'] for term in tax_terms), reverse=True)
    return result


def collect_news(now):
    queries = [
        ('미국주식·경제', '(미국 증시 OR 연준 OR 엔비디아 OR 미국 금리) when:2d', 6),
        ('부동산·세무', '(세제개편 OR 세법 OR 양도세 OR 부동산 정책 OR 국세청 OR 예규) '
         '(site:nts.go.kr OR site:mofe.go.kr OR site:moef.go.kr OR site:molit.go.kr '
         'OR site:law.go.kr OR site:tt.go.kr OR site:korea.kr) when:7d', 4),
    ]
    items, warnings = [], []
    cache_path = ROOT / '.state' / 'news-cache.json'
    try:
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
        if not isinstance(cached, list):
            cached = []
    except (OSError, ValueError):
        cached = []
    for group, query, limit in queries:
        urls = ['https://news.google.com/rss/search?' + urllib.parse.urlencode(
                    {'q': query, 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'}),
                'https://www.bing.com/news/search?' + urllib.parse.urlencode(
                    {'q': re.sub(r'\swhen:\d+d', '', query), 'format': 'rss', 'setlang': 'ko', 'cc': 'kr'})]
        found, errors = [], []
        for url in urls:
            try:
                found = parse_news(request(url), group, now)
                if found:
                    break
            except (RuntimeError, ET.ParseError, OSError, ValueError) as exc:
                errors.append(type(exc).__name__ + ': ' + str(exc))
        items.extend(found[:limit])
        if not found:
            reusable = []
            for item in cached:
                try:
                    date = datetime.fromisoformat(item['published_at'])
                    max_age = 7 if group == '부동산·세무' else 2
                    valid = item['group'] == group and now - timedelta(days=max_age) <= date <= now
                    valid = valid and urllib.parse.urlsplit(item['url']).scheme == 'https'
                    if valid:
                        reusable.append({**item, 'cached': True})
                except (ValueError, KeyError, TypeError):
                    continue
            items.extend(reusable[:limit])
            warnings.append(f'{group}: 현재 조회 제한 · 이전 조회 자료 사용(게시일 확인)' if reusable
                            else f'{group}: 확인된 뉴스 없음 / 조회 불가')
            print(group + ' RSS diagnostics: ' + '; '.join(errors))
    if items:
        cache_path.parent.mkdir(exist_ok=True)
        cache_path.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
    return items, warnings


def parse_market(payload, name, symbol, unit, now):
    result = payload['chart']['result'][0]
    meta = result['meta']
    stamp = int(meta['regularMarketTime'])
    observed = datetime.fromtimestamp(stamp, timezone.utc)
    price = float(meta['regularMarketPrice'])
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Invalid market values')
    if observed > now + timedelta(minutes=5):
        raise ValueError('Future market observation')
    history = [float(v) for v in result['indicators']['quote'][0]['close']
               if v is not None and math.isfinite(float(v))][-20:]
    # Query range is one month: use previous *session* close, not the month-start close.
    timestamps = result.get('timestamp', [])
    closes = result['indicators']['quote'][0]['close']
    exchange_zone = ZoneInfo(meta['exchangeTimezoneName'])
    session_date = observed.astimezone(exchange_zone).date()
    # Yahoo meta.previousClose is not consistently present. Daily history is authoritative here.
    eligible = [(t, float(v)) for t, v in zip(timestamps, closes) if v is not None
                and t < stamp and datetime.fromtimestamp(t, timezone.utc).astimezone(exchange_zone).date() < session_date
                and math.isfinite(float(v)) and float(v) > 0]
    if eligible:
        previous = eligible[-1][1]
    else:
        raise ValueError('Previous session close unavailable')
    change = (price / previous - 1) * 100
    delta = (price - previous) * 100 if unit == '%' else change
    return {'name': name, 'symbol': symbol, 'unit': unit, 'value': price,
            'change': delta, 'change_unit': 'bp' if unit == '%' else '%',
            'observed': observed.astimezone(KST).strftime('%m/%d %H:%M KST'),
            'stale': (now - observed).total_seconds() > 4 * 86400,
            'history': history, 'source': 'Yahoo Finance 공개 차트 API (지연 가능)',
            'url': 'https://finance.yahoo.com/quote/' + urllib.parse.quote(symbol, safe='')}


def collect_markets(now):
    result, warnings = [], []
    for name, symbol, unit in MARKETS:
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(symbol, safe='')
        try:
            payload = json_request(url + '?range=1mo&interval=1d')
            result.append(parse_market(payload, name, symbol, unit, now))
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, IndexError):
            warnings.append(f'{name}: 수치 확인 불가')
    return result, warnings


def font(size, bold=False):
    candidates = [
        Path('C:/Windows/Fonts/malgunbd.ttf' if bold else 'C:/Windows/Fonts/malgun.ttf'),
        Path('/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf' if bold
             else '/usr/share/fonts/truetype/nanum/NanumGothic.ttf')]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    raise RuntimeError('Korean font missing; install fonts-nanum')


def wrap(draw, text, face, width):
    lines, line = [], ''
    for char in text:
        if char == '\n' or (line and draw.textlength(line + char, font=face) > width):
            lines.append(line.rstrip())
            line = '' if char == '\n' else char
        else:
            line += char
    return lines + ([line.rstrip()] if line else [])


def render(report, path):
    ink, muted, accent = '#14283E', '#66788B', '#2463D5'
    image = Image.new('RGB', (1080, 4000), '#FFFFFF')
    draw = ImageDraw.Draw(image)
    body, small, title = font(28), font(21), font(46, True)
    draw.text((54, 42), 'MORNING BRIEF', fill=accent, font=font(23, True))
    draw.text((54, 82), '미국주식 · 부동산 · 세무', fill=ink, font=title)
    draw.text((54, 151), report['date'] + '  |  헤드라인 기반 · 테스트' if report.get('demo')
              else report['date'] + '  |  헤드라인 기반', fill=muted, font=small)
    y = 210
    draw.text((54, y), '시장 한눈에', fill=ink, font=font(31, True))
    y += 54
    for index, metric in enumerate(report['markets']):
        col, row = index % 2, index // 2
        x, top = 54 + col * 498, y + row * 173
        draw.rounded_rectangle((x, top, x + 474, top + 153), radius=14, fill='#F3F6FA')
        draw.text((x + 19, top + 14), metric['name'], fill=muted, font=small)
        value = f"{metric['value']:,.2f} {metric['unit']}"
        draw.text((x + 19, top + 45), value, fill=ink, font=font(29, True))
        change = f"{metric['change']:+.2f}{metric['change_unit']}"
        draw.text((x + 19, top + 85), change, fill=accent, font=font(23, True))
        draw.text((x + 19, top + 120), metric['observed'] + (' · 오래된 값' if metric['stale'] else ''), fill=muted, font=font(17))
        values = metric['history']
        if len(values) > 1 and max(values) != min(values):
            points = [(x + 320 + i * 130 / (len(values) - 1),
                       top + 100 - (v - min(values)) * 55 / (max(values) - min(values)))
                      for i, v in enumerate(values)]
            draw.line(points, fill=accent, width=3)
    y += math.ceil(len(report['markets']) / 2) * 173 + 23
    draw.text((54, y), '최근 일별 추이 · 단위와 기준 시각은 항목별 표시', fill=muted, font=small)
    y += 60
    for group in ('미국주식·경제', '부동산·세무'):
        draw.text((54, y), group, fill=ink, font=font(31, True))
        y += 57
        grouped = [(number, item) for number, item in enumerate(report['items'], 1) if item['group'] == group]
        if not grouped:
            draw.text((54, y), '확인된 뉴스 없음 · 아래 조회 상태 참고', fill=muted, font=body)
            y += 60
        for number, item in grouped:
            draw.text((54, y), f'{number:02}', fill=accent, font=font(27, True))
            lines = wrap(draw, item['text'], body, 900)
            for line in lines:
                draw.text((113, y), line, fill=ink, font=body)
                y += 41
            label = ' · 이전 조회 자료' if item.get('cached') else ' · 원문 확인'
            draw.text((113, y + 3), item['source'] + ' · ' + item['published'] + label, fill=muted, font=small)
            y += 49
        y += 25
    notes = ['기사 제목을 줄여 정리한 브리핑입니다. 원문 전문 분석은 포함하지 않습니다.',
             '세제개편안·입법예고는 확정 법률이 아닙니다. 적용일은 원문에서 확인하세요.',
             '시장: Yahoo Finance 공개 API(지연 가능) · 뉴스: 공개 뉴스 RSS / 공식 기관']
    notes += report['warnings']
    for note in notes:
        for line in wrap(draw, note, small, 970):
            draw.text((54, y), line, fill=muted, font=small)
            y += 31
    y += 30
    if y > image.height:
        raise RuntimeError('Report layout overflow')
    image.crop((0, 0, 1080, y)).save(path)


def write_report(report, directory):
    directory.mkdir(parents=True, exist_ok=True)
    stem = report['date']
    render(report, directory / (stem + '.png'))
    lines = [f"# {stem} 미국주식·세무 브리핑", '', f'![오늘의 그림]({stem}.png)', '',
             '공개 뉴스 제목 기반입니다. 개별 세무 판단·매매 추천이 아닙니다.', '']
    for i, item in enumerate(report['items'], 1):
        safe_title = item['text'].replace('[', '［').replace(']', '］')
        label = ' / 이전 조회 자료' if item.get('cached') else ''
        lines += [f"{i}. [{safe_title}]({item['url']}) — {item['source']} / {item['published']} KST{label}"]
    lines += ['', '## 시장 데이터', '']
    for metric in report['markets']:
        lines += [f"- [{metric['name']}]({metric['url']}): {metric['value']:,.2f} {metric['unit']}, "
                  f"{metric['change']:+.2f}{metric['change_unit']} / {metric['observed']} / {metric['source']}"]
    lines += ['', *report['warnings']]
    (directory / (stem + '.md')).write_text('\n'.join(lines), encoding='utf-8')
    (directory / (stem + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def encrypt_token(token, key):
    return base64.b64encode(SecretBox(base64.b64decode(key, validate=True)).encrypt(token.encode()))


def decrypt_token(ciphertext, key):
    return SecretBox(base64.b64decode(key, validate=True)).decrypt(base64.b64decode(ciphertext, validate=True)).decode()


def refresh_token(token, client_id, secret):
    data = {'grant_type': 'refresh_token', 'client_id': client_id, 'refresh_token': token}
    if secret:
        data['client_secret'] = secret
    value = json_request('https://kauth.kakao.com/oauth/token', data=data, form=True)
    if not value.get('access_token'):
        raise RuntimeError('Kakao token refresh failed; authorize again')
    return value['access_token'], value.get('refresh_token', token)


def commit_state(message):
    for args in [('config', 'user.name', 'daily-briefing[bot]'),
                 ('config', 'user.email', 'daily-briefing@users.noreply.github.com'),
                 ('add', '.state', 'docs'), ('commit', '-m', message), ('push', 'origin', 'HEAD:main')]:
        subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True)
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()


def build_template(report, repository, sha):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ValueError('Invalid repository or commit')
    path = f"docs/{report['date']}"
    # The default feed adds a button even when buttons is omitted.
    return {'template_id': '139046', 'template_args': {
        'IMAGE': f'https://raw.githubusercontent.com/{repository}/{sha}/{path}.png'}}


def send_message(access, template):
    result = json_request('https://kapi.kakao.com/v2/api/talk/memo/send',
                          data={'template_id': template['template_id'],
                                'template_args': json.dumps(template['template_args'], ensure_ascii=False)}, form=True,
                          headers={'Authorization': 'Bearer ' + access})
    if result.get('result_code') != 0:
        raise RuntimeError('Kakao did not confirm delivery; do not retry automatically')


def run_cloud(report, send):
    state = ROOT / '.state'
    attempt = state / (report['date'] + '.json')
    if send and attempt.exists():
        print('Already attempted today; no duplicate message sent.')
        return
    access = None
    if send:
        key = os.environ['TOKEN_KEY']
        token_path = state / 'kakao.enc'
        token = decrypt_token(token_path.read_bytes(), key)
        access, token = refresh_token(token, os.environ['KAKAO_REST_API_KEY'], os.environ.get('KAKAO_CLIENT_SECRET', ''))
        token_path.write_bytes(encrypt_token(token, key))
        # Persist refreshed credentials before collecting/publishing/sending can fail.
        commit_state('Persist encrypted Kakao credential')
    write_report(report, ROOT / 'docs')
    sha = commit_state('Publish public economic and tax briefing')
    if not send:
        print('Preview published; no Kakao message sent.')
        return
    template = build_template(report, os.environ['GITHUB_REPOSITORY'], sha)
    expected = hashlib.sha256((ROOT / 'docs' / (report['date'] + '.png')).read_bytes()).digest()
    for _ in range(6):
        try:
            if hashlib.sha256(request(template['template_args']['IMAGE'])).digest() == expected:
                break
        except (OSError, RuntimeError):
            pass
        time.sleep(5)
    else:
        raise RuntimeError('Published image is not reachable; no message sent')
    attempt.write_text(json.dumps({'status': 'attempting', 'at': datetime.now(KST).isoformat()}), encoding='utf-8')
    commit_state('Reserve daily delivery; never retry uncertain sends')
    send_message(access, template)
    attempt.write_text(json.dumps({'status': 'confirmed', 'at': datetime.now(KST).isoformat()}), encoding='utf-8')
    commit_state('Record Kakao API confirmed delivery')
    print('Kakao API result_code=0: one message sent.')


def is_delivery_day(now):
    day = now.astimezone(KST).date()
    return day.weekday() < 5 and day not in holidays.SouthKorea(years=day.year, observed=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cloud', action='store_true')
    args = parser.parse_args()
    now = datetime.now(KST)
    if args.cloud and not is_delivery_day(now):
        print('Korean weekend or public holiday: no briefing or Kakao message sent.')
        return
    markets, market_warnings = collect_markets(now)
    items, news_warnings = collect_news(now)
    report = {'date': now.strftime('%Y-%m-%d'), 'markets': markets, 'items': items,
              'warnings': market_warnings + news_warnings}
    if not report['items'] and not report['markets']:
        raise RuntimeError('All sources failed; no empty report or message will be sent')
    if args.cloud:
        run_cloud(report, os.environ.get('SEND', '').lower() == 'true')
    else:
        write_report(report, ROOT / 'preview')
        print('Local preview built; no message sent.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Failed safely: {type(exc).__name__}: {exc}')
        raise SystemExit(1)
