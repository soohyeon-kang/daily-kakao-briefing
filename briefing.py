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


def render(report, directory):
    ink, muted, accent = '#14283E', '#66788B', '#2463D5'
    pages = [('시장 한눈에', report['markets'][i:i + 3]) for i in range(0, len(report['markets']), 3)]
    for group in ('미국주식·경제', '부동산·세무'):
        grouped = [(number, item) for number, item in enumerate(report['items'], 1) if item['group'] == group]
        pages.extend((group, grouped[i:i + 2]) for i in range(0, len(grouped), 2))
    warnings = report['warnings']
    pages.extend(('조회 상태', warnings[i:i + 4]) for i in range(0, len(warnings), 4))
    paths = []
    for page, (group, entries) in enumerate(pages, 1):
        image = Image.new('RGB', (1080, 1080), '#FFFFFF')
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 1080, 12), fill=accent)
        draw.text((54, 43), 'MORNING BRIEF', fill=accent, font=font(27, True))
        draw.text((54, 96), group, fill=ink, font=font(55, True))
        label = report['date'] + (' · 테스트' if report.get('demo') else '')
        draw.text((54, 170), label, fill=muted, font=font(30))
        draw.text((900, 53), f'{page:02}/{len(pages):02}', fill=muted, font=font(28, True))
        if group == '시장 한눈에':
            for index, metric in enumerate(entries):
                top = 235 + index * 250
                draw.rounded_rectangle((54, top, 1026, top + 225), radius=20, fill='#F3F6FA')
                draw.text((80, top + 15), metric['name'], fill=muted, font=font(38, True))
                draw.text((80, top + 66), f"{metric['value']:,.2f} {metric['unit']}", fill=ink, font=font(56, True))
                draw.text((80, top + 135), f"{metric['change']:+.2f}{metric['change_unit']}", fill=accent, font=font(40, True))
                draw.text((80, top + 188), metric['observed'] + (' · 오래된 값' if metric['stale'] else ''), fill=muted, font=font(25))
                values = metric['history']
                if len(values) > 1 and max(values) != min(values):
                    points = [(780 + i * 215 / (len(values) - 1),
                               top + 170 - (v - min(values)) * 55 / (max(values) - min(values)))
                              for i, v in enumerate(values)]
                    draw.line(points, fill=accent, width=5)
            footnote = 'Yahoo Finance · 지연 가능 · 그래프: 최근 일별 추이'
        elif group == '조회 상태':
            y = 240
            for note in entries:
                for line in wrap(draw, note, font(38), 960):
                    draw.text((54, y), line, fill=ink, font=font(38))
                    y += 52
                y += 34
            if y > 1000:
                raise RuntimeError('Status card layout overflow')
            footnote = '확인된 자료만 표시합니다. 조회 실패 시 항목이 줄어듭니다.'
        else:
            for index, (number, item) in enumerate(entries):
                top = 240 + index * 380
                draw.text((54, top), f'{number:02}', fill=accent, font=font(32, True))
                face = font(58, True)
                lines = wrap(draw, item['text'], face, 880)
                # shortcut: exceptionally long headlines use an ellipsis; full text stays in the report.
                if len(lines) > 4:
                    lines = lines[:4]
                    while draw.textlength(lines[-1] + '…', font=face) > 880:
                        lines[-1] = lines[-1][:-1]
                    lines[-1] += '…'
                for line_index, line in enumerate(lines):
                    draw.text((130, top + line_index * 72), line, fill=ink, font=face)
                source = item['source'][:27] + ('…' if len(item['source']) > 27 else '')
                draw.text((130, top + 294), source, fill=muted, font=font(28))
                date = item['published'] + (' · 이전 조회 자료' if item.get('cached') else '')
                draw.text((130, top + 333), date, fill=muted, font=font(28))
                if index == 0 and len(entries) > 1:
                    draw.line((54, top + 370, 1026, top + 370), fill='#E2E8F0', width=2)
            footnote = '기사 제목 기반 · 세제개편안은 확정 법률과 다릅니다.'
        draw.text((54, 1022), footnote, fill=muted, font=font(26))
        path = directory / f"{report['date']}-{page:02}.png"
        image.save(path)
        paths.append(path)
    return paths


def write_report(report, directory):
    directory.mkdir(parents=True, exist_ok=True)
    stem = report['date']
    cards = render(report, directory)
    report['cards'] = [path.name for path in cards]
    lines = [f"# {stem} 미국주식·세무 브리핑", '',
             *[f'![카드 {index}]({path.name})' for index, path in enumerate(cards, 1)], '',
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
    return cards


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


def build_template(card, repository, sha):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ValueError('Invalid repository or commit')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}-\d{2}\.png', card):
        raise ValueError('Invalid card filename')
    # The default feed adds a button even when buttons is omitted.
    return {'template_id': '139046', 'template_args': {
        'IMAGE': f'https://raw.githubusercontent.com/{repository}/{sha}/docs/{card}'}}


def send_message(access, template):
    result = json_request('https://kapi.kakao.com/v2/api/talk/memo/send',
                          data={'template_id': template['template_id'],
                                'template_args': json.dumps(template['template_args'], ensure_ascii=False)}, form=True,
                          headers={'Authorization': 'Bearer ' + access})
    if result.get('result_code') != 0:
        raise RuntimeError('Kakao did not confirm delivery; do not retry automatically')


def run_cloud(report, send, test=False):
    state = ROOT / '.state'
    attempt = state / (report['date'] + ('-cards-test' if test else '') + '.json')
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
    if test:
        report['demo'] = True
    cards = write_report(report, ROOT / 'docs')
    sha = commit_state('Publish public economic and tax briefing')
    if not send:
        print('Preview published; no Kakao message sent.')
        return
    templates = [build_template(card.name, os.environ['GITHUB_REPOSITORY'], sha) for card in cards]
    for card, template in zip(cards, templates):
        expected = hashlib.sha256(card.read_bytes()).digest()
        for _ in range(6):
            try:
                if hashlib.sha256(request(template['template_args']['IMAGE'])).digest() == expected:
                    break
            except (OSError, RuntimeError):
                pass
            time.sleep(5)
        else:
            raise RuntimeError('Published image is not reachable; no message sent')
    delivery = {'status': 'attempting', 'at': datetime.now(KST).isoformat(), 'total': len(cards), 'confirmed': 0}
    attempt.write_text(json.dumps(delivery), encoding='utf-8')
    commit_state('Reserve daily delivery; never retry uncertain sends')
    for index, template in enumerate(templates, 1):
        send_message(access, template)
        delivery.update(confirmed=index, status='confirmed' if index == len(cards) else 'attempting')
        attempt.write_text(json.dumps(delivery), encoding='utf-8')
        commit_state(f'Record Kakao card {index}/{len(cards)} confirmed delivery')
        print(f'Kakao API result_code=0: card {index}/{len(cards)} sent.')


def is_delivery_day(now):
    day = now.astimezone(KST).date()
    return day.weekday() < 5 and day not in holidays.SouthKorea(years=day.year, observed=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cloud', action='store_true')
    parser.add_argument('--test', action='store_true', help='One separate test card set per day')
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
        run_cloud(report, os.environ.get('SEND', '').lower() == 'true', test=args.test)
    else:
        write_report(report, ROOT / 'preview')
        print('Local preview built; no message sent.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Failed safely: {type(exc).__name__}: {exc}')
        raise SystemExit(1)
