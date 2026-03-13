"""
네이버 부동산 아파트 조회 서버
- 검색/매물/실거래가: Playwright 브라우저 내 fetch() 사용
  - 검색: new.land.naver.com/api/search (쿠키 인증, JWT 불필요)
  - 매물/실거래가: JWT Bearer 사용
"""
import asyncio
import json
import threading
import time
import os
import urllib.parse

from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='public')


# ══════════════════════════════════════════════════════════
# Playwright 브라우저 매니저
# ══════════════════════════════════════════════════════════
class NaverBrowser:
    WARMUP_COMPLEX = '111515'
    JWT_TTL = 2.5 * 3600

    def __init__(self):
        self._loop   = asyncio.new_event_loop()
        self._ready  = threading.Event()
        self._jwt    = None
        self._jwt_ts = 0
        self._page   = None
        self._lock   = None  # asyncio.Lock 은 이벤트루프 안에서 생성

        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        if not self._ready.wait(timeout=90):
            raise RuntimeError('Playwright 초기화 시간 초과')
        print('[browser] 준비 완료')

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init())
        self._loop.run_forever()

    async def _init(self):
        self._lock = asyncio.Lock()
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().__aenter__()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._ctx = await self._browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            locale='ko-KR',
        )
        self._page = await self._ctx.new_page()

        async def on_request(req):
            if 'new.land.naver.com/api' in req.url:
                auth = req.headers.get('authorization', '')
                if auth.startswith('Bearer '):
                    tok = auth[7:]
                    if tok != self._jwt:
                        self._jwt = tok
                        self._jwt_ts = time.time()
                        print(f'[browser] JWT 갱신: {tok[:30]}…')
        self._page.on('request', on_request)

        print('[browser] 초기화 중 (단지 페이지 방문)…')
        await self._page.goto(
            f'https://new.land.naver.com/complexes/{self.WARMUP_COMPLEX}',
            wait_until='networkidle',
        )
        await asyncio.sleep(2)
        self._ready.set()

    async def _ensure_jwt(self):
        if self._jwt and (time.time() - self._jwt_ts) < self.JWT_TTL:
            return
        print('[browser] JWT 재발급 중…')
        await self._page.goto(
            f'https://new.land.naver.com/complexes/{self.WARMUP_COMPLEX}',
            wait_until='networkidle',
        )
        await asyncio.sleep(2)

    async def _eval_fetch(self, url: str, with_auth: bool = True) -> dict:
        await self._ensure_jwt()
        url_js = url.replace('\\', '\\\\').replace("'", "\\'")
        jwt_js = (self._jwt or '').replace("'", "\\'")
        auth_line = f"'Authorization': 'Bearer {jwt_js}'," if with_auth else ''
        result = await self._page.evaluate(f'''async () => {{
            const resp = await fetch('{url_js}', {{
                headers: {{
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'ko-KR,ko;q=0.9',
                    {auth_line}
                    'Referer': 'https://new.land.naver.com/'
                }}
            }});
            const text = await resp.text();
            let data;
            try {{
                data = JSON.parse(text);
            }} catch(e) {{
                return {{ status: resp.status, parseError: e.message, preview: text.substring(0, 120) }};
            }}
            return {{ status: resp.status, data }};
        }}''')
        if 'parseError' in result:
            raise Exception(
                f"JSON 파싱 실패 (HTTP {result['status']}): {result.get('preview','')[:100]}"
            )
        if result['status'] not in (200, 201):
            raise Exception(f"Naver API 오류 {result['status']}: {json.dumps(result.get('data',{}), ensure_ascii=False)[:120]}")
        return result['data']

    async def _locked_fetch(self, url, with_auth=True):
        async with self._lock:
            return await self._eval_fetch(url, with_auth=with_auth)

    def fetch(self, url: str, params: dict | None = None, with_auth: bool = True) -> dict:
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None and str(v) != ''},
                quote_via=urllib.parse.quote,
            )
            if qs:
                url = f'{url}?{qs}'
        future = asyncio.run_coroutine_threadsafe(
            self._locked_fetch(url, with_auth=with_auth), self._loop
        )
        return future.result(timeout=25)



_browser: NaverBrowser | None = None


def get_browser() -> NaverBrowser:
    global _browser
    if _browser is None:
        _browser = NaverBrowser()
    return _browser


# ══════════════════════════════════════════════════════════
# Flask 라우트
# ══════════════════════════════════════════════════════════

@app.route('/')
def index():
    return send_from_directory('public', 'index.html')


# ── 단지 검색 ────────────────────────────────────────────
@app.route('/api/search')
def search():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify({'error': '검색어를 입력하세요.'}), 400
    try:
        data = get_browser().fetch(
            'https://new.land.naver.com/api/search',
            params={'keyword': query, 'type': 'COMPLEX'},
            with_auth=True,
        )
        raw = data.get('complexes') or []
        complexes = [
            {
                'complexNo':           c.get('complexNo', ''),
                'complexName':         c.get('complexName', ''),
                'address':             c.get('cortarAddress', ''),
                'totalHouseholdCount': c.get('totalHouseholdCount', ''),
                'useApproveYmd':       c.get('useApproveYmd', ''),
            }
            for c in raw
        ]
        return jsonify({'complexes': complexes})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 단지 개요 + 평형 목록 ────────────────────────────────
@app.route('/api/complex/<complex_no>/overview')
def complex_overview(complex_no):
    url = f'https://new.land.naver.com/api/complexes/overview/{complex_no}'
    try:
        data = get_browser().fetch(url, {'complexNo': complex_no})
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 현재 매물 목록 (호가) ────────────────────────────────
@app.route('/api/articles/<complex_no>')
def articles(complex_no):
    trade_type = request.args.get('tradeType', 'A1')
    area_nos   = request.args.get('areaNos', '')
    page_no    = request.args.get('page', '1')
    url = f'https://new.land.naver.com/api/articles/complex/{complex_no}'
    params = {
        'realEstateType': 'APT:ABYG:JGC:PRE',
        'tradeType': trade_type,
        'tag': '::::::::',
        'rentPriceMin': '0', 'rentPriceMax': '900000000',
        'priceMin': '0',     'priceMax': '900000000',
        'areaMin': '0',      'areaMax': '900000000',
        'showArticle': 'false',
        'sameAddressGroup': 'false',
        'priceType': 'RETAIL',
        'page': page_no,
        'complexNo': complex_no,
        'buildingNos': '',
        'areaNos': area_nos,
        'type': 'list',
        'order': 'rank',
    }
    try:
        return jsonify(get_browser().fetch(url, params))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 실거래가 ─────────────────────────────────────────────
@app.route('/api/real-prices/<complex_no>')
def real_prices(complex_no):
    trade_type = request.args.get('tradeType', 'A1')
    area_no    = request.args.get('areaNo', '0')
    url = f'https://new.land.naver.com/api/complexes/{complex_no}/prices/real'
    params = {
        'complexNo': complex_no,
        'tradeType': trade_type,
        'year': '3',
        'priceChartChange': 'false',
        'areaNo': area_no,
        'type': 'table',
    }
    try:
        return jsonify(get_browser().fetch(url, params))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════
if __name__ == '__main__':
    print('[startup] 브라우저 초기화 중…')
    get_browser()
    port = int(os.environ.get('PORT', 3333))
    print(f'[startup] 서버: http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
