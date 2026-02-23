"""
AssetRipper - Backend Server
Run: python server.py
Then open: http://localhost:5000
"""

from flask import Flask, request, jsonify, send_from_directory
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re
import os
import json

app = Flask(__name__, static_folder='.')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def resolve_url(href, base):
    if not href:
        return None
    href = href.strip()
    if href.startswith(('data:', 'javascript:', '#', 'mailto:')):
        return None
    try:
        return urljoin(base, href)
    except Exception:
        return None


def fetch_url(url, timeout=10):
    try:
        r = SESSION.get(url, timeout=timeout, allow_redirects=True, verify=False)
        content_type = r.headers.get('Content-Type', '')
        size = len(r.content)
        
        # Try decode as text
        try:
            text = r.text
        except Exception:
            text = r.content.decode('latin-1', errors='replace')

        return {
            'ok': True,
            'text': text,
            'size': size,
            'status': r.status_code,
            'content_type': content_type,
            'final_url': r.url,
        }
    except requests.exceptions.SSLError:
        # Retry without SSL verify
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True, verify=False)
            return {'ok': True, 'text': r.text, 'size': len(r.content),
                    'status': r.status_code, 'content_type': r.headers.get('Content-Type',''), 'final_url': r.url}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def get_asset_type(url, content_type=''):
    url_lower = url.lower().split('?')[0]
    ct = content_type.lower()

    if 'javascript' in ct or url_lower.endswith(('.js', '.mjs', '.cjs')):
        return 'js'
    if 'css' in ct or url_lower.endswith('.css'):
        return 'css'
    if any(url_lower.endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.avif', '.bmp')):
        return 'img'
    if 'image' in ct:
        return 'img'
    if any(url_lower.endswith(ext) for ext in ('.woff', '.woff2', '.ttf', '.otf', '.eot')):
        return 'font'
    if 'font' in ct:
        return 'font'
    if url_lower.endswith(('.html', '.htm')) or 'html' in ct:
        return 'html'
    if url_lower.endswith(('.json',)):
        return 'json'
    return 'other'


def parse_assets_from_html(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    assets = []
    seen = set()

    def add(url, type_, tag, extra=None):
        if not url or url in seen:
            return
        seen.add(url)
        a = {'url': url, 'type': type_, 'tag': tag, 'inline': False}
        if extra:
            a.update(extra)
        assets.append(a)

    # ── CSS ──────────────────────────────────────────
    for el in soup.find_all('link', rel=lambda r: r and 'stylesheet' in r):
        u = resolve_url(el.get('href'), base_url)
        add(u, 'css', 'link[rel=stylesheet]')

    for el in soup.find_all('link', href=True):
        href = el.get('href', '')
        if '.css' in href:
            u = resolve_url(href, base_url)
            add(u, 'css', 'link[href*.css]')

    # Inline styles
    for i, el in enumerate(soup.find_all('style')):
        content = el.get_text()
        if content.strip():
            assets.append({
                'url': f'inline-style-{i+1}',
                'type': 'css', 'tag': '<style> inline',
                'inline': True, 'content': content, 'size': len(content)
            })
            # Extract URLs from inline CSS
            for m in re.finditer(r'url\(["\']?([^"\')\s]+)["\']?\)', content):
                u = resolve_url(m.group(1), base_url)
                if u:
                    ext = u.lower().split('?')[0].split('.')[-1]
                    t = 'font' if ext in ('woff','woff2','ttf','otf','eot') else 'img'
                    add(u, t, 'css url()')

    # ── JavaScript ────────────────────────────────────
    for el in soup.find_all('script', src=True):
        u = resolve_url(el.get('src'), base_url)
        add(u, 'js', 'script[src]', {
            'async': el.has_attr('async'),
            'defer': el.has_attr('defer'),
            'type': el.get('type', '')
        })

    for i, el in enumerate(soup.find_all('script', src=False)):
        content = el.get_text()
        if content.strip() and len(content) > 20:
            assets.append({
                'url': f'inline-script-{i+1}',
                'type': 'js', 'tag': '<script> inline',
                'inline': True, 'content': content[:10000], 'size': len(content)
            })

    # ── Images ────────────────────────────────────────
    for el in soup.find_all('img'):
        u = resolve_url(el.get('src'), base_url)
        add(u, 'img', 'img[src]', {'alt': el.get('alt','')})
        # srcset
        srcset = el.get('srcset', '')
        for part in srcset.split(','):
            parts = part.strip().split(); u2 = resolve_url(parts[0], base_url) if parts else None
            add(u2, 'img', 'img[srcset]')

    for el in soup.find_all(True):
        # background attribute
        bg = el.get('background')
        if bg:
            u = resolve_url(bg, base_url)
            add(u, 'img', 'background attr')
        # data-src (lazy load)
        for attr in ('data-src', 'data-lazy', 'data-original', 'data-url'):
            v = el.get(attr)
            if v and ('.' in v):
                u = resolve_url(v, base_url)
                add(u, 'img', f'{attr} (lazy)')

    # CSS background images from all style attributes
    for el in soup.find_all(style=True):
        for m in re.finditer(r'url\(["\']?([^"\')\s]+)["\']?\)', el['style']):
            u = resolve_url(m.group(1), base_url)
            add(u, 'img', 'style attr url()')

    # Background URLs in all HTML
    for m in re.finditer(r'url\(["\']?([^"\')\s]+)["\']?\)', html):
        candidate = m.group(1)
        u = resolve_url(candidate, base_url)
        if u:
            ext = u.lower().split('?')[0].split('.')[-1]
            t = 'font' if ext in ('woff','woff2','ttf','otf','eot') else 'img'
            add(u, t, 'html url()')

    # ── Fonts ─────────────────────────────────────────
    for el in soup.find_all('link', href=True):
        href = el.get('href','')
        if 'font' in href.lower() or any(ext in href.lower() for ext in ('.woff','.ttf','.otf','.eot')):
            u = resolve_url(href, base_url)
            add(u, 'font', 'link[font]')

    # Google Fonts
    for el in soup.find_all('link', href=re.compile(r'fonts\.(googleapis|gstatic)\.com')):
        u = resolve_url(el.get('href'), base_url)
        add(u, 'font', 'Google Fonts')

    # ── Meta / other ──────────────────────────────────
    # Favicon
    for el in soup.find_all('link', rel=lambda r: r and ('icon' in r or 'shortcut' in r)):
        u = resolve_url(el.get('href'), base_url)
        add(u, 'img', 'favicon')

    # OG image
    og_img = soup.find('meta', property='og:image')
    if og_img:
        u = resolve_url(og_img.get('content'), base_url)
        add(u, 'img', 'og:image')

    # Preload hints
    for el in soup.find_all('link', rel='preload'):
        u = resolve_url(el.get('href'), base_url)
        as_type = el.get('as', 'other')
        type_map = {'script':'js','style':'css','image':'img','font':'font'}
        add(u, type_map.get(as_type,'other'), 'link[preload]')

    return assets


def detect_libraries(html, all_urls):
    search = html + '\n' + '\n'.join(all_urls)
    found = []

    LIBS = [
        ('React',         '⚛️',  [r'react\.(?:min\.)?js', r'react[@/](\d+\.\d+)', r'__react', r'_reactFiber']),
        ('Next.js',       '▲',   [r'_next/static', r'__NEXT_DATA__', r'next[@/](\d+)']),
        ('Vue\.js',       '💚',  [r'vue\.(?:min\.)?js', r'vue[@/](\d+\.\d+)', r'__vue']),
        ('Angular',       '🅰️',  [r'angular\.(?:min\.)?js', r'@angular/core', r'ng-version']),
        ('Nuxt\.js',      '💚',  [r'_nuxt/', r'__NUXT', r'nuxt[@/]']),
        ('Svelte',        '🔥',  [r'svelte[@/]', r'__svelte', r'\.svelte\.']),
        ('Ember\.js',     '🐹',  [r'ember\.(?:min\.)?js', r'EmberENV']),
        ('Three\.js',     '🎮',  [r'three\.(?:min\.)?js', r'three[@/]r?(\d+)', r'THREE\.REVISION']),
        ('GSAP',          '🎞️',  [r'gsap\.(?:min\.)?js', r'gsap[@/](\d+\.\d+)', r'TweenMax', r'TweenLite']),
        ('jQuery',        '💲',  [r'jquery[.\-](\d+\.\d+)', r'jquery\.min\.js', r'jQuery\.fn']),
        ('Bootstrap',     '🅱️',  [r'bootstrap\.(?:min\.)?(?:js|css)', r'bootstrap[@/](\d+)']),
        ('Tailwind',      '🌊',  [r'tailwind(?:css)?\.(?:min\.)?css', r'tailwindcss[@/]']),
        ('Bulma',         '💪',  [r'bulma\.(?:min\.)?css', r'bulma[@/]']),
        ('Material UI',   '🎨',  [r'@mui/', r'material-ui', r'MuiButton']),
        ('Chakra UI',     '⚡',  [r'@chakra-ui/', r'chakra-ui']),
        ('Ant Design',    '🐜',  [r'ant-design', r'antd[@/]', r'antd\.min']),
        ('D3\.js',        '📊',  [r'd3\.(?:min\.)?js', r'd3[@/]v(\d+)', r'd3\.select']),
        ('Chart\.js',     '📈',  [r'chart\.(?:min\.)?js', r'chart\.js[@/]', r'ChartJS']),
        ('Plotly',        '📉',  [r'plotly\.(?:min\.)?js', r'plotly[@/]']),
        ('PIXI\.js',      '🎨',  [r'pixi\.(?:min\.)?js', r'pixi\.js[@/]', r'PIXI\.Application']),
        ('Babylon\.js',   '🏛️',  [r'babylon\.(?:max\.)?js', r'babylonjs[@/]', r'BABYLON\.']),
        ('p5\.js',        '✏️',  [r'p5\.(?:min\.)?js', r'p5[@/](\d+)', r'new p5\(']),
        ('A-Frame',       '🥽',  [r'aframe\.(?:min\.)?js', r'aframe[@/]', r'a-scene']),
        ('Phaser',        '🕹️',  [r'phaser\.(?:min\.)?js', r'phaser[@/]', r'Phaser\.Game']),
        ('Socket\.io',    '🔌',  [r'socket\.io(?:\.min)?\.js', r'socket\.io[@/]']),
        ('Axios',         '🌐',  [r'axios\.(?:min\.)?js', r'axios[@/](\d+)']),
        ('Lodash',        '🔧',  [r'lodash\.(?:min\.)?js', r'lodash[@/](\d+)']),
        ('Moment\.js',    '⏰',  [r'moment\.(?:min\.)?js', r'moment[@/](\d+)']),
        ('Framer Motion', '🎭',  [r'framer-motion', r'motion[@/](\d+)']),
        ('Lottie',        '🎬',  [r'lottie\.(?:min\.)?js', r'lottie-web', r'lottie[@/]']),
        ('Alpine\.js',    '🏔️',  [r'alpine\.(?:min\.)?js', r'alpinejs[@/]', r'x-data=']),
        ('Stimulus',      '⚡',  [r'stimulus[@/]', r'@hotwired/stimulus']),
        ('Htmx',          '🔄',  [r'htmx\.(?:min\.)?js', r'htmx[@/]', r'hx-get=']),
        ('Webpack',       '📦',  [r'webpackJsonp', r'webpackChunk', r'webpack-runtime', r'\.chunk\.js']),
        ('Vite',          '⚡',  [r'/@vite/', r'vite[@/](\d+)', r'__vite_']),
        ('Parcel',        '📦',  [r'parcel[@/]', r'parcelRequire']),
        ('Rollup',        '📦',  [r'rollup[@/]', r'ROLLUP_']),
        ('esbuild',       '⚡',  [r'esbuild[@/]', r'// node_modules/.pnpm']),
        ('TypeScript',    '🔷',  [r'typescript[@/]', r'\.tsx?\.js']),
        ('WordPress',     '🔵',  [r'wp-content/', r'wp-includes/', r'wordpress']),
        ('Shopify',       '🛍️',  [r'cdn\.shopify\.com', r'shopify\.com/s/files']),
        ('Wix',           '🌐',  [r'static\.wixstatic\.com', r'wix\.com']),
        ('Webflow',       '🌊',  [r'webflow\.com', r'Webflow\.']),
        ('Squarespace',   '⬛',  [r'squarespace\.com', r'sqspcdn\.com']),
        ('Google Tag Mgr','📌',  [r'googletagmanager\.com', r'GTM-']),
        ('Google Analytics','📉',[r'google-analytics\.com', r'gtag/js', r'ga\.js']),
        ('Google Fonts',  '🔤',  [r'fonts\.googleapis\.com', r'fonts\.gstatic\.com']),
        ('Cloudflare',    '☁️',  [r'cdnjs\.cloudflare\.com', r'cloudflare\.com']),
        ('AWS CloudFront','☁️',  [r'cloudfront\.net']),
        ('jsDelivr',      '📦',  [r'cdn\.jsdelivr\.net']),
        ('unpkg',         '📦',  [r'unpkg\.com']),
        ('Sentry',        '🔍',  [r'sentry\.io', r'@sentry/', r'Sentry\.init']),
        ('Intercom',      '💬',  [r'intercom\.io', r'Intercom\(']),
        ('Hotjar',        '🔥',  [r'hotjar\.com', r'hj\(']),
        ('Stripe',        '💳',  [r'js\.stripe\.com', r'Stripe\(']),
        ('reCAPTCHA',     '🤖',  [r'recaptcha', r'google\.com/recaptcha']),
    ]

    for name, icon, patterns in LIBS:
        matches = sum(1 for p in patterns if re.search(p, search, re.IGNORECASE))
        if matches > 0:
            # Try to extract version
            version = None
            for p in patterns:
                m = re.search(p, search, re.IGNORECASE)
                if m and m.lastindex:
                    version = m.group(1)
                    break
            found.append({
                'name': name.replace('\\.', '.'),
                'icon': icon,
                'version': version or 'detected',
                'confidence': 'high' if matches >= 2 else 'medium'
            })

    return found


# ─── API ROUTES ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/extract', methods=['POST'])
def extract():
    data = request.get_json()
    url = data.get('url', '').strip()
    fetch_content = data.get('fetch_content', True)
    max_js = data.get('max_js', 10)

    if not url:
        return jsonify({'error': 'URL required'}), 400

    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    # Fetch HTML
    result = fetch_url(url)
    if not result['ok']:
        return jsonify({'error': f"Failed to fetch {url}: {result.get('error')}"}), 502

    html = result['text']
    final_url = result['final_url']

    # Parse assets
    assets = parse_assets_from_html(html, final_url)

    # Detect libraries
    all_urls = [a['url'] for a in assets if not a.get('inline')]
    libraries = detect_libraries(html, all_urls)

    # Fetch content for CSS and JS
    if fetch_content:
        css_assets = [a for a in assets if a['type'] == 'css' and not a.get('inline')]
        js_assets  = [a for a in assets if a['type'] == 'js'  and not a.get('inline')]

        for a in css_assets:
            r = fetch_url(a['url'])
            if r['ok']:
                a['content'] = r['text'][:50000]  # 50KB max
                a['size'] = r['size']
            else:
                a['fetch_error'] = r.get('error', 'failed')

        for a in js_assets[:max_js]:
            r = fetch_url(a['url'])
            if r['ok']:
                a['content'] = r['text'][:100000]  # 100KB max
                a['size'] = r['size']
            else:
                a['fetch_error'] = r.get('error', 'failed')

    return jsonify({
        'url': url,
        'final_url': final_url,
        'html_size': len(html),
        'html_preview': html[:5000],
        'assets': assets,
        'libraries': libraries,
        'stats': {
            'total': len(assets),
            'html':  sum(1 for a in assets if a['type'] == 'html'),
            'css':   sum(1 for a in assets if a['type'] == 'css'),
            'js':    sum(1 for a in assets if a['type'] == 'js'),
            'img':   sum(1 for a in assets if a['type'] == 'img'),
            'font':  sum(1 for a in assets if a['type'] == 'font'),
            'other': sum(1 for a in assets if a['type'] == 'other'),
        }
    })


@app.route('/api/fetch', methods=['POST'])
def fetch_single():
    """Fetch a single asset's content"""
    data = request.get_json()
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL required'}), 400
    r = fetch_url(url)
    if r['ok']:
        return jsonify({'content': r['text'][:200000], 'size': r['size'], 'content_type': r['content_type']})
    return jsonify({'error': r.get('error')}), 502


@app.route('/api/download', methods=['POST'])
def download_file():
    """Download all assets to local folder"""
    import urllib.parse, time
    data = request.get_json()
    assets = data.get('assets', [])
    folder = data.get('folder', f'assets_{int(time.time())}')
    base_url = data.get('base_url', '')

    os.makedirs(folder, exist_ok=True)
    results = []

    for a in assets:
        if a.get('inline') or not a.get('url','').startswith('http'):
            continue
        try:
            r = fetch_url(a['url'])
            if r['ok']:
                # Build filename
                path = urlparse(a['url']).path
                fname = os.path.basename(path) or 'index.html'
                # Make safe
                fname = re.sub(r'[^\w\-_\.]', '_', fname)
                fpath = os.path.join(folder, fname)
                mode = 'w' if isinstance(r['text'], str) else 'wb'
                with open(fpath, mode, encoding='utf-8' if mode == 'w' else None, errors='replace' if mode=='w' else None) as f:
                    f.write(r['text'])
                results.append({'url': a['url'], 'saved': fpath, 'size': r['size']})
        except Exception as e:
            results.append({'url': a['url'], 'error': str(e)})

    return jsonify({'folder': folder, 'results': results, 'count': len(results)})


if __name__ == '__main__':
    import urllib3
    urllib3.disable_warnings()
    print("\n" + "═"*50)
    print("  AssetRipper Server")
    print("  Open: http://localhost:5000")
    print("═"*50 + "\n")
    app.run(debug=False, port=5000, host='0.0.0.0')
