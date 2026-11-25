#!/usr/bin/env python3
"""Extract and verify this single public member page. No recursive crawling.

prepare: read the saved HTML and write a curl resource list.
finalize: verify downloaded files and create readable/structured exports + ZIP.
Network requests are performed separately with curl; no credentials are used.
"""
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/hkeid-sidneychang'
SOURCE = 'https://hkeid.com/member/sidneychang'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.images, self.meta, self.text = [], [], [], []
        self.in_script, self.in_style = False, False
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.in_script, self.buffer = True, []
        elif tag == 'style':
            self.in_style = True
        elif tag == 'img':
            if attrs.get('src'):
                self.images.append(attrs['src'])
            if attrs.get('alt'):
                self.text.append(attrs['alt'])
        elif tag == 'meta':
            self.meta.append(attrs)

    def handle_data(self, value):
        if self.in_script:
            self.buffer.append(value)
        elif not self.in_style and value.strip():
            self.text.append(value.strip())

    def handle_endtag(self, tag):
        if tag == 'script':
            self.scripts.append(''.join(self.buffer))
            self.in_script = False
        elif tag == 'style':
            self.in_style = False


def parse_html(text):
    parser = PageParser()
    parser.feed(text)
    return parser


def find_profile(value):
    if isinstance(value, dict):
        if isinstance(value.get('profile'), dict):
            if value['profile'].get('nicename') == 'sidneychang':
                return value
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = find_profile(child)
        if found:
            return found


def prepare():
    parser = parse_html((OUT / 'raw/profile.html').read_text())
    flight = ''.join(json.loads(s[len('self.__next_f.push('):-1])[1]
                     for s in parser.scripts if s.startswith('self.__next_f.push([1,'))
    props = None
    for line in flight.splitlines():
        try:
            props = find_profile(json.loads(line.split(':', 1)[1]))
        except (ValueError, IndexError):
            continue
        if props:
            break
    if not props:
        raise ValueError('Sidney Chang public profile not found in page payload')
    profile = props['profile']
    write_json(OUT / 'raw/profile.json', profile)
    write_json(OUT / 'raw/page-metadata.json', parser.meta)
    (OUT / 'raw/page-text.txt').write_text('\n'.join(parser.text) + '\n')
    (OUT / 'raw/self-introduction.html').write_text(profile['contactProfiles.self_intro'])
    (OUT / 'raw/whatsapp-message.html').write_text(profile['contactProfiles.whatsapp_content'])
    active = profile['icons']['active']
    resources = {}

    def add(url, category, label):
        url = urljoin(SOURCE, url)
        if urlparse(url).path == '/_next/image':
            url = parse_qs(urlparse(url).query)['url'][0]
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in (
                'hkeid.com', 'data.hkeid.com', 'admin.hkeid.com', 's.w.org'):
            raise ValueError(f'Unexpected attachment host: {url}')
        if url in resources:
            return
        name = re.sub(r'[^a-zA-Z0-9._-]', '_', unquote(parsed.path.rsplit('/', 1)[-1]))
        path = f'assets/{category}/{hashlib.sha256(url.encode()).hexdigest()[:8]}-{name}'
        (OUT / path).parent.mkdir(parents=True, exist_ok=True)
        resources[url] = {'url': url, 'local_path': path, 'category': category, 'label': label}

    add(profile['contactProfiles.avatar'], 'profile', '個人頭像原圖')
    add(profile['contactProfiles.bg'], 'profile', '封面背景原圖')
    add(profile['socialMedia']['wechat']['img'], 'profile', 'WeChat QR code')
    for item in profile['socialMedia']['customs']:
        add('https://data.hkeid.com/avatar/' + item['img'].rsplit('/', 1)[-1],
            'brands', item['url'])
    for item in parser.meta:
        if item.get('property') == 'og:image':
            add(item['content'], 'profile', '分享預覽頭像')
    for url in parser.images:
        add(url, 'page-icons', '此頁顯示圖示')
    for icon in props['iconsAvailable']:
        if icon['key'] in active:
            add(icon['img'], 'page-icons', icon['key'])
    for field in ['contactProfiles.self_intro', 'contactProfiles.whatsapp_content']:
        for url in parse_html(profile[field]).images:
            add(url, 'page-icons', field)
    response = json.loads((OUT / 'raw/vcard-response.json').read_text())
    if not isinstance(response, str) or not re.fullmatch(r'sidneychang_[a-zA-Z0-9]+\.vcf', response):
        raise ValueError('Unexpected vCard filename')
    add('https://data.hkeid.com/vcards/' + response, 'contact', '目前下載按鈕提供的電子名片')
    add('https://data.hkeid.com/vcards/' + profile['contactProfiles.vcard'],
        'contact', '頁面原始欄位引用的電子名片')
    entries = list(resources.values())
    write_json(OUT / 'download-list.json', entries)
    config = []
    for entry in entries:
        config.extend(['url = ' + json.dumps(entry['url']),
                       'output = ' + json.dumps(str(OUT / entry['local_path']))])
    (ROOT / '.cache/hkeid-analysis/assets-curl.conf').write_text('\n'.join(config) + '\n')
    print(json.dumps({'profile_fields': len(profile), 'resources': len(entries)}, ensure_ascii=False))


def finalize():
    profile = json.loads((OUT / 'raw/profile.json').read_text())
    entries = json.loads((OUT / 'download-list.json').read_text())
    failures, unavailable, vcards = [], [], []
    for entry in entries:
        path = OUT / entry['local_path']
        if not path.exists():
            headers = OUT / 'raw/legacy-vcard-response-headers.txt'
            if (entry['url'].endswith('/' + profile['contactProfiles.vcard'])
                    and headers.exists() and ' 404 ' in headers.read_text()):
                entry.update(status='unavailable', http_status=404,
                             reason='The old vCard referenced in the embedded profile no longer exists.')
                unavailable.append(entry['url'])
            else:
                entry['status'] = 'missing'
                failures.append(entry['url'])
            continue
        data = path.read_bytes()
        suffix = path.suffix.lower()
        # Preserve original bytes and filenames even when the source extension is wrong.
        actual_format = ('PNG' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                         'JPEG' if data.startswith(b'\xff\xd8') else None)
        valid = ((suffix in ('.png', '.jpg', '.jpeg') and actual_format is not None) or
                 (suffix == '.svg' and b'<svg' in data[:2000]) or
                 (suffix == '.vcf' and data.startswith(b'BEGIN:VCARD') and b'END:VCARD' in data))
        if actual_format:
            entry['actual_image_format'] = actual_format
            entry['extension_matches_format'] = ((actual_format == 'PNG' and suffix == '.png') or
                                                   (actual_format == 'JPEG' and suffix in ('.jpg', '.jpeg')))
            from PIL import Image
            with Image.open(path) as im:
                entry['image_dimensions'] = list(im.size)
                im.verify()
            entry['image_decode_verified'] = True
        entry.update(status='verified' if valid else 'invalid', bytes=len(data),
                     sha256=hashlib.sha256(data).hexdigest())
        if not valid:
            failures.append(entry['url'])
        if suffix == '.vcf' and valid:
            vcards.append(entry)
    now = datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat(timespec='seconds')
    intro = '\n'.join(parse_html(profile['contactProfiles.self_intro']).text)
    whatsapp = '\n'.join(parse_html(profile['contactProfiles.whatsapp_content']).text)
    linked_sites = [{'type': 'personal_website', 'url': profile['url']},
                    {'type': 'facebook', 'url': profile['socialMedia']['facebook']}]
    linked_sites.extend({'type': 'brand_website', 'url': x['url'], 'icon_id': x['iconId']}
                        for x in profile['socialMedia']['customs'])
    write_json(OUT / 'external-links.json', linked_sites)
    fields = {key: profile.get('contactProfiles.' + key) for key in
              ['company', 'title', 'mobile', 'office_phone', 'address', 'whatsapp']}
    normalized = {'source_url': SOURCE, 'archived_at': now, 'display_name': profile['displayName'],
                  **fields, 'email': profile['email'], 'website': profile['url'],
                  'self_introduction_text': intro, 'whatsapp_message_text': whatsapp,
                  'external_links': linked_sites, 'active_icons': profile['icons']['active'],
                  'page_created_at_original': profile['createTime'],
                  'fax': None,
                  'notes': ['原始 fax 值只有 +852，未當作有效傳真號碼。',
                            '已保存目前下載按鈕提供的 vCard；頁面原始欄位引用的舊檔回傳 404，僅保留來源及失效記錄。',
                            '內容按來源保存，履歷及公司介紹未作獨立事實核實。']}
    write_json(OUT / 'profile.json', normalized)
    if vcards:
        (OUT / 'Sidney-Chang.vcf').write_bytes((OUT / vcards[0]['local_path']).read_bytes())
    summary = {'source_url': SOURCE, 'archived_at': now, 'scope': 'Only this member page and its direct attachments',
               'public_profile_top_level_fields': len(profile), 'resources': entries,
               'resource_count': len(entries), 'verified_count': sum(x['status'] == 'verified' for x in entries),
               'download_failures': failures, 'unavailable_source_urls': unavailable,
               'external_site_count': len(linked_sites),
               'external_sites_crawled': False, 'browser_visual_check': 'Unavailable: browser access host unavailable',
               'coverage': ['Server-rendered text and metadata', 'Full embedded public profile object',
                            'Profile images, active icon images and WeChat QR code',
                            'vCard button response and referenced files', 'External link destinations'],
               'limitations': ['No authenticated back-office records or analytics',
                               'No external websites or social feed contents',
                               'No live browser screenshot; dynamic resources inferred from the public page code',
                               'No pagination, gallery or video references found in this profile payload and rendering component']}
    write_json(OUT / 'manifest.json', summary)
    report = [profile['displayName'] + ' — 公開名片資料備份', '來源：' + SOURCE, '保存時間：' + now, '',
              '範圍：只包括 Sidney Chang 呢一頁及直接附屬資源；未爬其他會員或外部網站。', '',
              '姓名：' + profile['displayName'], '職位：' + fields['title'], '公司：' + fields['company'],
              '手機：' + fields['mobile'], '辦公室電話：' + fields['office_phone'],
              'WhatsApp：' + fields['whatsapp'], '電郵：' + profile['email'],
              '地址：' + fields['address'], '網站：' + profile['url'], '',
              '個人及公司介紹（原文）', intro, '', 'WhatsApp 預設訊息（只保存，未發送）', whatsapp, '',
              '頁面外部連結（只保存連結）']
    report.extend(x['type'] + '：' + x['url'] for x in linked_sites)
    report.extend(['', '檔案說明', 'profile.json：整理後資料', 'raw/profile.json：完整公開個人資料欄位，保留原值',
                   'raw/profile.html：原頁 HTML 快照（並非完整離線網站）',
                   'raw/self-introduction.html：原始介紹格式', 'raw/whatsapp-message.html：原始預設訊息格式',
                   'external-links.json：7 個外部網站連結', 'Sidney-Chang.vcf：下載按鈕目前提供的電子名片',
                   'assets/：圖片、品牌圖示、QR code 及目前可下載的電子名片',
                   'manifest.json：來源 URL、檔案位置、大小、SHA-256 及驗證結果', '',
                   f'下載驗證：{summary["verified_count"]}/{len(entries)} 個直接資源檔案通過格式檢查。',
                   f'來源失效：{len(unavailable)} 個；意外下載失敗：{len(failures)} 個。',
                   '內容按來源保存，履歷及公司介紹未作獨立事實核實。',
                   '瀏覽器連線不可用，因此未提供畫面截圖；已核對 HTML、公開資料及對應渲染程式。',
                   '未發現此人頁面的分頁、相簿或影片引用。',
                   '未包含登入後台、訪問統計、其他會員、外部網站或 Facebook 貼文。',
                   '原始 fax 值只有 +852，保留於 raw/profile.json，整理檔視為未提供。',
                   '舊電子名片 sidneychang_e64af.vcf 回傳 404；目前下載按鈕的電子名片已保存。',
                   '有三張圖片網址副檔名為 .png，但實際為 JPEG；已解碼驗證並保留來源檔名及原始位元組。', '',
                   '整理做法', '1. 保存原頁及完整公開個人資料，保留原文和來源。',
                   '2. 下載此人頭像、封面、WeChat QR code、品牌圖示及電子名片。',
                   '3. 輸出可閱讀 TXT、可再用 JSON 和 VCF，再打包 ZIP。',
                   '4. 日後如要重建個人網站，以 profile.json 為資料來源、assets 為媒體庫；本次未建站。'])
    (OUT / '閱讀說明.txt').write_text('\n'.join(report) + '\n')
    # Hash every exported file, including the raw snapshots and normalized records.
    hashes = []
    for path in sorted(OUT.rglob('*')):
        if path.is_file() and path.name != 'SHA256SUMS.txt':
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.relative_to(OUT).as_posix())
    (OUT / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n')
    archive = ROOT / 'data/sidneychang-public-data.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(OUT.rglob('*')):
            if path.is_file():
                zf.write(path, 'sidneychang/' + path.relative_to(OUT).as_posix())
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
    print(json.dumps({'verified': summary['verified_count'], 'expected': len(entries),
                      'unavailable': unavailable, 'failures': failures,
                      'zip_bytes': archive.stat().st_size}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    {'prepare': prepare, 'finalize': finalize}[sys.argv[1]]()
