#!/usr/bin/env python3
"""Resumable, read-only archive of the public Naturalia Shopline site.

Uses only linked pages, sitemap URLs, and their media; no login or admin APIs.
Requirements already present locally: lxml, Pillow. curl performs HTTP GETs.
"""
import ast
import concurrent.futures as futures
import hashlib
import html as html_std
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from lxml import html
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/naturalia-group'
BASE = 'https://www.naturalia-group.com'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'
ALLOWED = {'www.naturalia-group.com', 'naturaliagroup.shoplineapp.com'}
JSON_VALUE = re.compile(r"app\.value\('([^']+)',\s*JSON.parse\(('(?:\\.|[^'\\])*')\)")
MEDIA_HOSTS = {'img.shoplineapp.com', 'shoplineimg.com', 'static.shoplineapp.com',
               'cdn.shoplineapp.com', 'www.naturalia-group.com', 'naturaliagroup.shoplineapp.com'}
for sub in ['raw/pages', 'raw/products', 'text', 'content', 'assets', 'indexes']:
    (OUT / sub).mkdir(parents=True, exist_ok=True)


def now():
    return datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat(timespec='seconds')


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def token(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def normalize_page(url, parent=BASE + '/zh-hant/'):
    if not url or url.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
        return None
    p = urlsplit(urljoin(parent, html_std.unescape(url)))
    if p.hostname not in ALLOWED:
        return None
    path = unquote(p.path)
    if '{{' in path or '}}' in path:
        return None
    parts = path.strip('/').split('/')
    lang = parts.pop(0) if parts and parts[0] in ('en', 'zh-hant') else 'zh-hant'
    if parts and parts[0] and parts[0] not in ('products', 'categories', 'pages', 'about', 'blogs', 'posts', 'blog'):
        return None
    if parts == ['categories']:
        return None
    query = dict(parse_qsl(p.query))
    if any(k in query for k in ('query', 'draft', 'preview', 'theme_preview', 'debug')):
        return None
    page = query.get('page')
    query = {'page': page} if page and page.isdigit() and int(page) > 1 else {}
    path = '/' + lang + '/' + '/'.join(parts).rstrip('/')
    return urlunsplit(('https', 'www.naturalia-group.com', quote(path, safe='/-._~'), urlencode(query), ''))


def get(url, relative_path, max_seconds=50):
    path = OUT / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    started = now()
    result = subprocess.run(['curl', '-sS', '-L', '--http1.1', '--max-time', str(max_seconds),
                             '--retry', '1', '--retry-delay', '2', '-A', UA, '-o', str(path),
                             '-w', '%{json}', quote(url, safe=':/?=&%+;,.-_~')],
                            capture_output=True, text=True)
    try:
        info = json.loads(result.stdout)
    except ValueError:
        info = {}
    status = int(info.get('http_code', 0))
    record = {'url': url, 'local_path': relative_path, 'fetched_at': started,
              'http_status': status, 'effective_url': info.get('url_effective', url),
              'content_type': info.get('content_type'), 'curl_exit_code': result.returncode}
    if result.stderr:
        record['error'] = result.stderr[:500]
    if path.exists():
        data = path.read_bytes()
        record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    record['ok'] = status == 200 and result.returncode == 0 and record.get('bytes', 0) > 0
    if relative_path.startswith('assets/') and 'text/html' in (record.get('content_type') or ''):
        record.update(ok=False, error='Attachment URL returned HTML instead of an asset')
    time.sleep(0.12)
    return record


def document(path):
    return html.fromstring(path.read_text(errors='replace'))


def crawl():
    statefile = OUT / 'indexes/pages-download.json'
    state = load(statefile, {})
    seeds = set()
    sitemap_records = []
    for lang in ('en', 'zh-hant', 'default'):
        path = OUT / ('raw/discovery/sitemap-' + lang + '.xml')
        root = ET.fromstring(path.read_bytes())
        for n in root.findall('{*}url'):
            source = n.findtext('{*}loc')
            normalized = normalize_page(source)
            sitemap_records.append({'locale': lang, 'url': source, 'archive_url': normalized,
                                    'last_modified': n.findtext('{*}lastmod')})
            if normalized:
                seeds.add(normalized)
    dump(OUT / 'indexes/sitemap-urls.json', sitemap_records)
    seeds.update([BASE + '/en/products', BASE + '/zh-hant/products'])
    pending = set(seeds)
    scanned = set()
    completed = 0
    while pending:
        batch = sorted(pending - scanned)
        if not batch:
            break
        pending.clear()
        to_fetch = [u for u in batch if not state.get(u, {}).get('ok')]
        print(json.dumps({'phase': 'pages', 'queued': len(to_fetch), 'previously_saved': len(state)}, ensure_ascii=False), flush=True)
        with futures.ThreadPoolExecutor(max_workers=3) as pool:
            jobs = {pool.submit(get, u, 'raw/pages/' + token(u) + '.html'): u for u in to_fetch}
            for job in futures.as_completed(jobs):
                u = jobs[job]
                try:
                    state[u] = job.result()
                except Exception as exc:
                    state[u] = {'url': u, 'ok': False, 'error': str(exc)}
                completed += 1
                if completed % 20 == 0:
                    dump(statefile, state)
                    print(json.dumps({'phase': 'pages', 'saved': sum(x.get('ok', False) for x in state.values()),
                                      'attempted': len(state), 'remaining_batch': len(jobs) - completed % (len(jobs) + 1)}, ensure_ascii=False), flush=True)
        dump(statefile, state)
        for u in batch:
            scanned.add(u)
            r = state.get(u, {})
            if not r.get('ok'):
                continue
            try:
                tree = document(OUT / r['local_path'])
            except Exception:
                continue
            for href in tree.xpath('//a/@href'):
                v = normalize_page(href, u)
                if v and v not in scanned:
                    pending.add(v)
                if v:
                    other = v.replace('/zh-hant/', '/en/', 1) if '/zh-hant/' in v else v.replace('/en/', '/zh-hant/', 1)
                    if other not in scanned:
                        pending.add(other)
    dump(statefile, state)
    print(json.dumps({'phase': 'pages_complete', 'total': len(state),
                      'ok': sum(x.get('ok', False) for x in state.values()),
                      'failed': [u for u, x in state.items() if not x.get('ok')]}, ensure_ascii=False), flush=True)


def text_of(node):
    clone = html.fromstring(html.tostring(node, encoding='unicode'))
    for n in clone.xpath('//script|//style|//noscript|//template'):
        n.drop_tree()
    # Preserve paragraph boundaries while keeping words split across inline spans intact.
    marker = '\ue000'
    for n in clone.iter():
        if n.tag in ('div', 'p', 'section', 'article', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr'):
            n.text = marker + (n.text or '')
            n.tail = marker + (n.tail or '')
        elif n.tag == 'br':
            n.tail = marker + (n.tail or '')
    lines = [re.sub(r'\s+', ' ', t).strip() for t in clone.text_content().split(marker)]
    return '\n'.join(t for t in lines if t and '{{' not in t and '}}' not in t)


def extract():
    state = load(OUT / 'indexes/pages-download.json', {})
    pages, products, media, external = [], {}, {}, {}
    def add_media(url, page, role='page_media'):
        if not url or '{{' in url or url.startswith('data:'):
            return
        url = html_std.unescape(url).replace('\\/', '/').replace('\\u0026', '&')
        p = urlsplit(urljoin(page, url))
        if p.hostname not in MEDIA_HOSTS or p.scheme not in ('http', 'https'):
            return
        # Platform scripts, tracking pixels, captcha, and fonts are not site content.
        ext = Path(p.path).suffix.lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.avif', '.pdf', '.mp4', '.webm', '.mov', '.mp3', '.xlsx', '.docx', '.pptx', '.zip'):
            return
        key = urlunsplit(('https', p.netloc, p.path, p.query, ''))
        # Ignore cache timestamps for deduplication, but preserve the original URL.
        clip = re.search(r'/media/image_clips/([a-f0-9]{24})/', p.path)
        transformed = re.search(r'^/[a-f0-9]{24}/([a-f0-9]{24})/', p.path) if p.hostname == 'shoplineimg.com' else None
        clip_id = (clip or transformed).group(1) if clip or transformed else None
        identity = 'shopline-image:' + clip_id if clip_id else key
        size = re.search(r'/(\d+)x', p.path)
        rank = 1000000 if clip and '/original.' in p.path else int(size.group(1)) if size else 0
        if identity not in media:
            media[identity] = {'url': key, 'source_urls': [], 'roles': [],
                               'variant_urls': [], 'selection_rank': rank,
                               'image_clip_id': clip_id,
                               'local_path': 'assets/' + token(identity) + ext}
        item = media[identity]
        if rank > item['selection_rank']:
            item.update(url=key, selection_rank=rank, local_path='assets/' + token(identity) + ext)
        if key not in item['variant_urls']:
            item['variant_urls'].append(key)
        if page not in item['source_urls']:
            item['source_urls'].append(page)
        if role not in item['roles']:
            item['roles'].append(role)

    for u, r in sorted(state.items()):
        if not r.get('ok'):
            continue
        raw = (OUT / r['local_path']).read_text(errors='replace')
        tree = html.fromstring(raw)
        title = tree.findtext('.//title') or ''
        if 'The site is now busy' in title:
            r.update(ok=False, error='Site returned busy/error HTML')
            continue
        lang = urlsplit(u).path.split('/')[1]
        part = urlsplit(u).path.split('/')[2:]
        kind = part[0] if part and part[0] else 'home'
        body = tree.xpath('//*[@id="Content"]') or tree.xpath('//main') or tree.xpath('//body')
        node = body[0] if body else tree
        text = text_of(node)
        key = token(u)
        (OUT / ('text/' + key + '.txt')).write_text(text + '\n')
        (OUT / ('content/' + key + '.html')).write_text(html.tostring(node, encoding='unicode'))
        ld = []
        for script in tree.xpath('//script[@type="application/ld+json"]'):
            try:
                ld.append(json.loads(script.text or ''))
            except ValueError:
                pass
        meta = {m.get('property') or m.get('name'): m.get('content') for m in tree.xpath('//meta[@content]')}
        record = {'url': u, 'language': lang, 'kind': kind, 'title': title,
                  'canonical': (tree.xpath('//link[@rel="canonical"]/@href') or [None])[0],
                  'meta_description': meta.get('description'), 'structured_data': ld,
                  'raw_path': r['local_path'], 'text_path': 'text/' + key + '.txt',
                  'content_html_path': 'content/' + key + '.html', 'fetched_at': r['fetched_at']}
        pages.append(record)
        for attr in ('src', 'data-src', 'data-original', 'ng-src', 'poster'):
            for val in tree.xpath(f'//img/@{attr}|//video/@{attr}|//source/@{attr}'):
                add_media(val, u)
        for val in tree.xpath('//meta[@property="og:image"]/@content'):
            add_media(val, u, 'og_image')
        for val in tree.xpath('//@style'):
            for match in re.finditer(r'url\([\s\'\"]*([^\)\'\"]+)', val):
                add_media(match.group(1), u, 'background')
        # Direct originals embedded in page/widget configuration, including lazy-loaded images.
        decoded = raw.replace('\\/', '/').replace('\\u0026', '&')
        for match in re.finditer(r'https?://(?:img\.shoplineapp\.com|shoplineimg\.com)/[^\s\"\'<>\\)]+', decoded):
            add_media(match.group(), u)
        for a in tree.xpath('//a[@href]|//iframe[@src]|//video[@src]|//source[@src]'):
            val = a.get('href') or a.get('src')
            target = urljoin(u, html_std.unescape(val))
            parsed = urlsplit(target)
            add_media(target, u, 'linked_attachment')
            if parsed.scheme in ('http', 'https') and parsed.hostname not in ALLOWED:
                external.setdefault(target, {'url': target, 'source_urls': [], 'element': a.tag})['source_urls'].append(u)
        for match in JSON_VALUE.finditer(raw):
            if match.group(1) != 'product':
                continue
            try:
                product = json.loads(ast.literal_eval(match.group(2)))
            except (ValueError, SyntaxError):
                continue
            pid = product.get('_id')
            if not pid:
                continue
            dump(OUT / f'raw/products/{pid}-{lang}.json', product)
            p = products.setdefault(pid, {'id': pid, 'sku': product.get('sku'),
                                          'title_translations': product.get('title_translations'),
                                          'summary_translations': product.get('summary_translations'),
                                          'price': product.get('price'), 'sale_price': product.get('price_sale'),
                                          'quantity_as_exposed_publicly': product.get('quantity'),
                                          'sold_out': product.get('sold_out'), 'is_preorder': product.get('is_preorder'),
                                          'variations': product.get('variations'), 'variant_options': product.get('variant_options'),
                                          'media': product.get('media'), 'pages': [], 'raw_json_paths': []})
            p['pages'].append(record)
            p['raw_json_paths'].append(f'raw/products/{pid}-{lang}.json')
            description = tree.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " ProductDetail-description ")]')
            if description:
                p.setdefault('description_by_language', {})[lang] = {
                    'text': text_of(description[0]),
                    'html': html.tostring(description[0], encoding='unicode')}
            for m in product.get('media', []):
                original = m.get('images', {}).get('original', {}).get('url')
                if original:
                    add_media(original, u, 'product_original')
            for m in product.get('cover_media_array', []):
                add_media(m.get('original_image_url'), u, 'product_original')
    # One original (or largest linked size) per image; every variant URL remains in the index.
    dump(OUT / 'pages.json', pages)
    dump(OUT / 'products.json', list(products.values()))
    dump(OUT / 'indexes/media-discovered.json', list(media.values()))
    for x in external.values():
        x['source_urls'] = sorted(set(x['source_urls']))
    dump(OUT / 'external-links.json', list(external.values()))
    print(json.dumps({'phase': 'extracted', 'pages': len(pages), 'products': len(products),
                      'media': len(media), 'external_links': len(external), 'kinds': Counter(x['kind'] for x in pages)}, ensure_ascii=False), flush=True)


def download_assets():
    items = load(OUT / 'indexes/media-discovered.json', [])
    file = OUT / 'indexes/assets-download.json'
    state = load(file, {})
    for record in state.values():
        if 'text/html' in (record.get('content_type') or ''):
            record.update(ok=False, error='Attachment URL redirected to HTML instead of an image')
    for item in items:
        if state.get(item['url'], {}).get('ok'):
            state[item['url']].update({k: item[k] for k in
                                      ['source_urls', 'roles', 'variant_urls', 'selection_rank', 'image_clip_id']})
    pending = [x for x in items if not state.get(x['url'], {}).get('ok')]
    print(json.dumps({'phase': 'assets', 'queued': len(pending), 'already_saved': len(state)}), flush=True)
    def fetch_asset(item):
        previous = state.get(item['url'], {})
        result = previous if previous.get('error', '').startswith('Attachment URL') else get(item['url'], item['local_path'], 70)
        parsed = urlsplit(item['url'])
        legacy_path = re.sub(r'^/(en|zh-hant)/images/', '/images/', parsed.path)
        if not result.get('ok') and parsed.hostname == 'www.naturalia-group.com' and legacy_path.startswith('/images/'):
            legacy_url = urlunsplit(('https', 'naturalia-group.com', legacy_path, parsed.query, ''))
            recovered = get(legacy_url, item['local_path'], 70)
            if recovered.get('ok'):
                result = {**recovered, 'url': item['url'], 'download_url': legacy_url,
                          'recovered_from_legacy_host': True,
                          'original_response': {k: result.get(k) for k in ['http_status', 'effective_url', 'content_type', 'error']}}
        return result
    with futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(fetch_asset, x): x for x in pending}
        for i, job in enumerate(futures.as_completed(jobs), 1):
            item = jobs[job]
            try:
                result = job.result()
            except Exception as exc:
                result = {'url': item['url'], 'ok': False, 'error': str(exc)}
            state[item['url']] = {**item, **result}
            if i % 30 == 0:
                dump(file, state)
                print(json.dumps({'phase': 'assets', 'saved': sum(x.get('ok', False) for x in state.values()),
                                  'processed': i, 'remaining': len(pending) - i}), flush=True)
    dump(file, state)
    print(json.dumps({'phase': 'assets_complete', 'total': len(state),
                      'ok': sum(x.get('ok', False) for x in state.values())}), flush=True)


def finalize():
    pages = load(OUT / 'pages.json', [])
    products = load(OUT / 'products.json', [])
    page_downloads = load(OUT / 'indexes/pages-download.json', {})
    asset_downloads = load(OUT / 'indexes/assets-download.json', {})
    invalid = []
    for u, r in asset_downloads.items():
        if not r.get('ok'):
            continue
        path = OUT / r['local_path']
        data = path.read_bytes()
        if path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif'):
            try:
                with Image.open(path) as im:
                    r.update(actual_image_format=im.format, image_dimensions=list(im.size))
                    im.verify()
                r['decode_verified'] = True
            except Exception as exc:
                r.update(decode_verified=False, verification_error=str(exc))
                invalid.append(u)
        elif path.suffix == '.svg' and b'<svg' not in data:
            invalid.append(u)
        elif path.suffix == '.pdf' and not data.startswith(b'%PDF'):
            invalid.append(u)
    dump(OUT / 'indexes/assets-download.json', asset_downloads)
    sitemap = load(OUT / 'indexes/sitemap-urls.json', [])
    expected = set(x['archive_url'] for x in sitemap if x['archive_url'])
    downloaded = {x['url'] for x in pages}
    summary = {'source': BASE + '/', 'archived_at': now(), 'public_only': True,
               'languages': dict(Counter(x['language'] for x in pages)),
               'page_types': dict(Counter(x['kind'] for x in pages)), 'page_count': len(pages),
               'product_count': len(products), 'sitemap_unique_language_pages': len(expected),
               'sitemap_pages_missing': sorted(expected - downloaded),
               'page_download_failures': [x for x in page_downloads.values() if not x.get('ok')],
               'asset_count': sum(x.get('ok', False) for x in asset_downloads.values()),
               'asset_bytes': sum(x.get('bytes', 0) for x in asset_downloads.values() if x.get('ok')),
               'asset_download_failures': [x for x in asset_downloads.values() if not x.get('ok')],
               'images_recovered_from_legacy_host': [x['url'] for x in asset_downloads.values() if x.get('recovered_from_legacy_host')],
               'invalid_assets': invalid,
               'scope': 'Current www.naturalia-group.com public Shopline site, both languages, all sitemap entries and linked public pages/pagination.',
               'excluded': ['Authenticated member/admin/order/customer data', 'External social media feeds and third-party websites',
                            'Videos embedded from external streaming services (embed URLs retained)',
                            'Legacy non-www Joomla pages; only directly referenced broken images may be recovered from the matching old-host paths'],
               'notes': ['Prices and stock are a point-in-time public snapshot.',
                         'Product raw JSON is data embedded in public product HTML, not an authenticated database export.',
                         'The default sitemap duplicates the localized content; entries are mapped to zh-hant.',
                         'One original image or the largest linked size is downloaded per image clip; variant URLs are preserved.',
                         'This is a content archive, not a fully functioning offline shop.']}
    dump(OUT / 'manifest.json', summary)
    lines = ['Naturalia Group 公開網站備份', '來源：' + BASE + '/', '完成時間：' + summary['archived_at'], '',
             f'公開頁面：{len(pages)}，商品：{len(products)}，附件／圖片：{summary["asset_count"]}',
             '語言：' + json.dumps(summary['languages'], ensure_ascii=False),
             '網站地圖尚未下載：' + str(len(summary['sitemap_pages_missing'])),
             '失敗頁面：' + str(len(summary['page_download_failures'])),
             '失敗附件：' + str(len(summary['asset_download_failures'])),
             '由同公司舊網址補回的原有圖片：' + str(len(summary['images_recovered_from_legacy_host'])),
             '格式驗證失敗：' + str(len(invalid)), '',
             'products.json：商品、SKU、中英文名稱、簡介、價格、規格、公開庫存狀態及圖片來源。',
             'pages.json：每頁網址、標題、語言、SEO 資料、結構化資料、原始檔／文字／內容 HTML 位置。',
             'raw/pages/：原始公開 HTML；raw/products/：商品頁內嵌公開 JSON。',
             'text/：可讀文字；content/：保留格式的主內容 HTML；assets/：圖片和直接附件。',
             'indexes/：網站地圖、來源與下載對照表，含 HTTP 狀態、檔案大小及 SHA-256。',
             'external-links.json：外部網站及嵌入影片連結。',
             'manifest.json：範圍、完整性及失敗清單。', '',
             '登入資料尚未提供，因此本備份不含會員後台、訂單、客戶名單或其他私人資料。',
             '外部社交網站、串流影片本體及其他品牌網站未下載；已保留引用連結。',
             '無 www 的 naturalia-group.com 回傳舊 Joomla 網站，與指定的新 Shopline 網站不同；未混入商品資料。',
             '圖片按原始圖像 ID 去重，下載原圖或頁面最大可用版本；各尺寸來源仍保留於 indexes/media-discovered.json。',
             '價格和庫存只代表下載當刻；原文中的產品宣稱並未另行核實。',
             '此備份用於保存內容及日後重建，並非可離線運作的購物系統。', '', '頁面索引：']
    lines.extend(f'{x["language"]} | {x["title"]} | {x["url"]} | {x["text_path"]}' for x in pages)
    (OUT / '閱讀說明.txt').write_text('\n'.join(lines) + '\n')
    hashes = []
    for path in sorted(OUT.rglob('*')):
        if path.is_file() and path.name != 'SHA256SUMS.txt':
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + str(path.relative_to(OUT)))
    (OUT / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n')
    archive = OUT.parent / 'naturalia-group-public-data.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(OUT.rglob('*')):
            if path.is_file():
                zf.write(path, 'naturalia-group/' + path.relative_to(OUT).as_posix())
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
    print(json.dumps({**{k: summary[k] for k in ['page_count', 'product_count', 'asset_count', 'languages', 'page_types']},
                      'missing_sitemap': len(summary['sitemap_pages_missing']),
                      'failed_pages': len(summary['page_download_failures']),
                      'failed_assets': len(summary['asset_download_failures']),
                      'invalid_assets': len(invalid), 'zip_bytes': archive.stat().st_size}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    {'crawl': crawl, 'extract': extract, 'assets': download_assets, 'finalize': finalize}[sys.argv[1]]()
