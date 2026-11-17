import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nacl.exceptions import CryptoError
from PIL import Image
import briefing as b


class BriefingTests(unittest.TestCase):
    def test_korean_weekends_and_public_holidays_are_excluded(self):
        for day in ('2026-11-21', '2026-12-25', '2026-02-17', '2026-08-17',
                    '2026-07-17', '2026-06-03', '2025-01-27'):
            with self.subTest(day=day):
                self.assertFalse(b.is_delivery_day(datetime.fromisoformat(day + 'T09:00:00+09:00')))
        self.assertTrue(b.is_delivery_day(datetime.fromisoformat('2026-11-18T09:00:00+09:00')))

    def test_delivery_day_uses_korean_date(self):
        self.assertFalse(b.is_delivery_day(datetime.fromisoformat('2026-11-20T15:30:00+00:00')))
        self.assertTrue(b.is_delivery_day(datetime.fromisoformat('2026-11-22T15:30:00+00:00')))

    def test_cloud_holiday_stops_before_data_collection(self):
        with patch.object(b, 'is_delivery_day', return_value=False), \
             patch('sys.argv', ['briefing.py', '--cloud']), \
             patch.object(b, 'collect_markets') as markets, patch.object(b, 'run_cloud') as cloud:
            b.main()
            markets.assert_not_called()
            cloud.assert_not_called()

    def test_empty_http_response_is_valid(self):
        with patch.object(b, 'request', return_value=b''):
            self.assertEqual(b.json_request('https://api.github.com/test'), {})

    def test_encrypt_roundtrip_and_wrong_key(self):
        key = base64.b64encode(b'k' * 32).decode()
        encrypted = b.encrypt_token('private-token', key)
        self.assertNotIn(b'private-token', encrypted)
        self.assertEqual(b.decrypt_token(encrypted, key), 'private-token')
        with self.assertRaises(CryptoError):
            b.decrypt_token(encrypted, base64.b64encode(b'x' * 32).decode())

    def test_refresh_preserves_or_replaces_refresh_token(self):
        with patch.object(b, 'json_request', return_value={'access_token': 'access'}):
            self.assertEqual(b.refresh_token('old', 'key', 'secret'), ('access', 'old'))
        with patch.object(b, 'json_request', return_value={'access_token': 'access', 'refresh_token': 'new'}):
            self.assertEqual(b.refresh_token('old', 'key', 'secret'), ('access', 'new'))

    def test_tax_domains_are_exact(self):
        self.assertTrue(b.is_tax_source('https://www.nts.go.kr/'))
        self.assertFalse(b.is_tax_source('https://nts.go.kr.evil.example/'))

    def test_bing_rss_keeps_original_official_link(self):
        xml = b'''<rss xmlns:News="https://www.bing.com/news"><channel><item>
        <title>Tax law amendment announcement</title>
        <link>https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fwww.nts.go.kr%2Fnotice</link>
        <pubDate>Mon, 16 Nov 2026 03:00:00 GMT</pubDate><News:Source>NTS</News:Source>
        </item></channel></rss>'''
        result = b.parse_news(xml, '부동산·세무', datetime(2026, 11, 17, tzinfo=timezone.utc))
        self.assertEqual(result[0]['url'], 'https://www.nts.go.kr/notice')
        self.assertEqual(result[0]['source'], 'NTS')

    def test_news_cache_is_marked_and_expired_articles_are_not_reused(self):
        now = datetime(2026, 11, 17, tzinfo=timezone.utc)
        item = {'group': '부동산·세무', 'text': '세제개편안 공개 안내', 'url': 'https://www.nts.go.kr/news',
                'source': '국세청', 'published': '11/16 09:00', 'published_at': '2026-11-16T00:00:00+00:00'}
        with tempfile.TemporaryDirectory() as temp, patch.object(b, 'ROOT', Path(temp)):
            state = Path(temp) / '.state'
            state.mkdir()
            cache = state / 'news-cache.json'
            cache.write_text(json.dumps([item, {**item, 'published_at': '2026-10-01T00:00:00+00:00'}]), encoding='utf-8')
            with patch.object(b, 'request', side_effect=RuntimeError('HTTP 429')):
                items, warnings = b.collect_news(now)
            self.assertEqual(len(items), 1)
            self.assertTrue(items[0]['cached'])
            self.assertIn('이전 조회 자료', warnings[1])

    def test_news_filters_future_old_unofficial_and_duplicates(self):
        now = datetime(2026, 11, 17, 3, tzinfo=timezone.utc)
        item = '<item><title>{}</title><link>https://news.google.com/test</link><pubDate>{}</pubDate><source url="{}">기관</source></item>'
        valid = item.format('세제개편안 공개 안내', 'Mon, 16 Nov 2026 03:00:00 GMT', 'https://www.mofe.go.kr')
        xml = '<rss><channel>' + valid + valid
        xml += item.format('가짜', 'Mon, 16 Nov 2026 03:00:00 GMT', 'https://example.com')
        xml += item.format('미래', 'Mon, 30 Nov 2026 03:00:00 GMT', 'https://www.nts.go.kr')
        xml += item.format('과거', 'Mon, 02 Nov 2026 03:00:00 GMT', 'https://www.nts.go.kr')
        xml += item.format('undefined상세', 'Mon, 16 Nov 2026 03:00:00 GMT', 'https://www.nts.go.kr')
        xml += '</channel></rss>'
        result = b.parse_news(xml.encode(), '부동산·세무', now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['text'], '세제개편안 공개 안내')

    def test_us_close_uses_exchange_day_not_korean_day(self):
        stamp = lambda text: int(datetime.fromisoformat(text).timestamp())
        payload = {'chart': {'result': [{'meta': {'regularMarketTime': stamp('2026-11-16T21:00:00+00:00'),
                   'regularMarketPrice': 98, 'exchangeTimezoneName': 'America/New_York'},
                   'timestamp': [stamp('2026-11-13T14:30:00+00:00'), stamp('2026-11-16T14:30:00+00:00')],
                   'indicators': {'quote': [{'close': [100, 98]}]}}]}}
        now = datetime(2026, 11, 17, 3, tzinfo=timezone.utc)
        result = b.parse_market(payload, 'S&P 500', '^GSPC', 'pt', now)
        self.assertAlmostEqual(result['change'], -2)
        self.assertEqual(result['observed'], '11/17 06:00 KST')
        payload['chart']['result'][0]['meta']['regularMarketPrice'] = 4.1
        payload['chart']['result'][0]['indicators']['quote'][0]['close'] = [4, 4.1]
        self.assertAlmostEqual(b.parse_market(payload, '금리', '^TNX', '%', now)['change'], 10)

    def test_uncertain_send_does_not_retry(self):
        with patch.object(b, 'json_request', side_effect=RuntimeError('timeout')) as call:
            with self.assertRaises(RuntimeError):
                b.send_message('secret', b.build_template({'date': '2026-11-17'}, 'owner/repo', 'a' * 40))
            self.assertEqual(call.call_count, 1)

    def test_custom_image_template_and_send_endpoint(self):
        template = b.build_template({'date': '2026-11-17'}, 'owner/repo', 'a' * 40)
        self.assertEqual(template['template_id'], '139046')
        self.assertEqual(list(template['template_args']), ['IMAGE'])
        self.assertTrue(template['template_args']['IMAGE'].endswith('/docs/2026-11-17.png'))
        with patch.object(b, 'json_request', return_value={'result_code': 0}) as call:
            b.send_message('secret', template)
            self.assertEqual(call.call_args.args[0], 'https://kapi.kakao.com/v2/api/talk/memo/send')
            self.assertEqual(json.loads(call.call_args.kwargs['data']['template_args']), template['template_args'])
        with self.assertRaises(ValueError):
            b.build_template({'date': '2026-11-17'}, 'https://bad', 'a' * 40)

    def test_existing_attempt_blocks_send_before_credentials(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(b, 'ROOT', Path(temp)):
            state = Path(temp) / '.state'
            state.mkdir()
            (state / '2026-11-17.json').write_text('{"status":"attempting"}')
            with patch.object(b, 'refresh_token') as refresh:
                b.run_cloud({'date': '2026-11-17'}, True)
                refresh.assert_not_called()

    def test_failed_persistence_never_sends(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(b, 'ROOT', Path(temp)):
            key = base64.b64encode(b'k' * 32).decode()
            state = Path(temp) / '.state'
            state.mkdir()
            (state / 'kakao.enc').write_bytes(b.encrypt_token('old', key))
            with patch.dict(b.os.environ, {'TOKEN_KEY': key, 'KAKAO_REST_API_KEY': 'key'}), \
                 patch.object(b, 'refresh_token', return_value=('access', 'new')), \
                 patch.object(b, 'commit_state', side_effect=RuntimeError('push failed')), \
                 patch.object(b, 'send_message') as send:
                with self.assertRaises(RuntimeError):
                    b.run_cloud({'date': '2026-11-17'}, True)
                send.assert_not_called()

    def test_kakao_rejects_nonzero_result(self):
        with patch.object(b, 'json_request', return_value={'result_code': -1}):
            with self.assertRaises(RuntimeError):
                b.send_message('secret', b.build_template({'date': '2026-11-17'}, 'owner/repo', 'a' * 40))

    def test_render_ten_long_headlines_without_cropping(self):
        report = {'date': '2026-11-17', 'demo': True, 'markets': [], 'warnings': ['테스트 데이터'],
                  'items': [{'group': '미국주식·경제' if i < 6 else '부동산·세무',
                             'text': '한글 뉴스 제목의 줄바꿈과 여백을 검증하는 긴 테스트 문장입니다. ' * 2,
                             'source': '테스트', 'published': '11/17 09:00',
                             'url': 'https://example.com'} for i in range(10)]}
        with tempfile.TemporaryDirectory() as temp:
            b.write_report(report, Path(temp))
            with Image.open(Path(temp) / '2026-11-17.png') as image:
                self.assertEqual(image.width, 1080)
                self.assertGreater(image.height, 1500)
                self.assertLess(image.height, 4000)


if __name__ == '__main__':
    unittest.main()
