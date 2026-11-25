#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a private, inert archive from explicitly saved Chrome MHTML files.

No network calls, browser/session storage access, or credentials are needed.
UI observations supply live form values that MHTML does not serialize.
"""
from copy import deepcopy
from datetime import datetime
from email import policy
from email.parser import BytesParser
from html import escape, unescape
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse
import zipfile
from zoneinfo import ZoneInfo

from lxml import etree, html

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache/naturalia-member'
OUT = ROOT / 'data/naturalia-group-member'
PUBLIC = ROOT / 'data/naturalia-group'
LABELS = ['個人資料', '商店購物金', '優惠券', '訊息', '訂單', '追蹤清單',
          '領券中心', '商品目錄第 1 頁', '商品目錄第 2 頁', '商品目錄第 3 頁',
          '商品詳情抽查', '一萬元購物金', '三萬元購物金', '購物車']
STYLE = '''body{max-width:1100px;margin:40px auto;padding:0 24px;font:16px/1.65 system-ui,sans-serif;color:#202020;background:#fafafa}h1{font-size:28px}h2{font-size:22px}a{color:#275c91}table{border-collapse:collapse;width:100%;background:white}td,th{padding:9px 12px;border:1px solid #ddd;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#fff;padding:18px}small,.note{color:#666}button,input,select,textarea{font:inherit;max-width:100%;margin:4px;padding:5px}section{margin:28px 0}.source{overflow-wrap:anywhere}.raw img{display:none}'''


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def document(title, body):
    return '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; form-action \'none\'; base-uri \'none\'"><title>' + escape(title) + '</title><style>' + STYLE + '</style></head><body>' + body + '</body></html>'


def load_snapshot(path):
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    part = next(p for p in msg.walk() if p.get_content_type() == 'text/html')
    dom = html.fromstring(part.get_payload(decode=True).decode('utf-8'))
    return msg.get('Snapshot-Content-Location') or part.get('Content-Location'), dom


def sanitize(node):
    node = deepcopy(node)
    for e in list(node.iterdescendants()):
        if e.getparent() is None:
            continue
        if not isinstance(e.tag, str):
            e.getparent().remove(e)
            continue
        cls = set(e.get('class', '').split())
        style = e.get('style', '').replace(' ', '').lower()
        if (e.tag in {'script', 'iframe', 'object', 'embed', 'svg', 'link', 'meta', 'style'}
            or cls.intersection({'ng-hide', 'hide', 'hidden', 'country-list', 'grecaptcha-badge', 'g-recaptcha-response'})
            or 'display:none' in style or 'visibility:hidden' in style
            or e.get('type') in {'password', 'hidden', 'file'}):
            e.getparent().remove(e)
            continue
        if e.tag == 'img':
            alt, src = e.get('alt', ''), e.get('data-src') or e.get('src', '')
            if src.startswith('https://'):
                e.tag = 'a'
                e.attrib.clear()
                e.set('href', src)
                e.text = '[圖片來源' + (': ' + alt if alt else '') + ']'
            else:
                e.getparent().remove(e)
            continue
        if e.tag == 'form':
            e.tag = 'div'
        allowed = {'class', 'id', 'href', 'alt', 'title', 'name', 'value', 'type', 'placeholder',
                   'selected', 'checked', 'colspan', 'rowspan', 'data-currency-hkd', 'data-currency-gbp'}
        for attr in list(e.attrib):
            if attr not in allowed:
                del e.attrib[attr]
        if e.get('href') and not e.get('href').startswith(('https://', 'http://', '#')):
            del e.attrib['href']
        if e.tag in {'input', 'select', 'textarea', 'button'}:
            e.set('disabled', 'disabled')
    for attr in list(node.attrib):
        if attr not in {'id', 'class'}:
            del node.attrib[attr]
    return node


def text_lines(node):
    lines = []
    for value in node.itertext():
        value = ' '.join(value.split())
        if value:
            lines.append(value)
    for e in node.xpath('.//input[@value]'):
        if e.get('value'):
            lines.append((e.get('name') or '欄位') + ': ' + e.get('value'))
    return '\n'.join(lines)


def cents(label):
    if not label:
        return None
    return round(float(re.sub(r'[^0-9.]', '', label)) * 100)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for directory in [OUT / 'raw', OUT / 'text']:
        directory.mkdir(exist_ok=True)
    observations = json.loads((CACHE / 'ui-observations.json').read_text(encoding='utf-8'))
    dump(OUT / 'member-data.json', observations)
    public = {p['id']: p for p in json.loads((PUBLIC / 'products.json').read_text(encoding='utf-8'))}
    sources, catalogue = [], []
    snapshots = sorted(CACHE.glob('[0-9][0-9]-*.mhtml'))
    assert len(snapshots) == 14, len(snapshots)
    for path, label in zip(snapshots, LABELS):
        url, dom = load_snapshot(path)
        number = int(path.name[:2])
        if number <= 6:
            node = dom.get_element_by_id(f'tabset-tab-MEMBER_CENTER-{number}')
            assert 'active' in node.get('class', '').split()
        elif number == 14:
            node = dom.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," CartPanel ")]')[0]
        else:
            node = dom.get_element_by_id('Content')
        clean = sanitize(node)
        prefix = '<h1>' + label + '</h1><p class="source">來源：<a href="' + escape(url, quote=True) + '">' + escape(url) + '</a></p><p class="note">2026-11-16 香港時間；已移除程式、驗證元件及可提交表單。圖片原檔另見公開備份。</p>'
        if number == 1:
            prefix += '<p class="note">瀏覽器快照未儲存部分表單即時值；以下欄位由已顯示的帳戶畫面核對。</p><pre>' + escape(json.dumps(observations['account'], ensure_ascii=False, indent=2)) + '</pre>'
        serialized = html.tostring(clean, encoding='unicode')
        (OUT / 'raw' / (path.stem + '.html')).write_text(document(label, prefix + '<div class="raw">' + serialized + '</div>'), encoding='utf-8')
        text = label + '\n來源：' + url + '\n\n' + text_lines(clean) + '\n'
        if number == 1:
            text += '\n已從畫面核對的欄位：\n' + json.dumps(observations['account'], ensure_ascii=False, indent=2) + '\n'
        if number == 3:
            text += '\n已逐一核對可使用、即將開始、已失效：三類均無優惠券。\n'
        if number == 4:
            text += '\n畫面顯示：' + observations['sections']['messages']['send_status'] + '\n'
        (OUT / 'text' / (path.stem + '.txt')).write_text(text, encoding='utf-8')
        sources.append({'label': label, 'url': url, 'source_mhtml': path.name,
                        'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'captured_at': datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo('Asia/Hong_Kong')).isoformat(),
                        'html': 'raw/' + path.stem + '.html', 'text': 'text/' + path.stem + '.txt'})
        if number in {8, 9, 10}:
            for a in dom.xpath('//a[@ga-product]'):
                p = json.loads(a.get('ga-product'))
                prices = a.xpath('.//*[@data-currency-hkd]')
                price = prices[0] if prices else None
                catalogue.append({'id': p['id'], 'sku': p.get('sku'), 'title': p.get('title'), 'url': a.get('href'),
                                  'variants': json.loads(p.get('variations', '[]')),
                                  'display_currency': 'GBP', 'display_price': unescape(price.get('data-currency-gbp', '')) if price is not None else None,
                                  'base_hkd_price': price.get('data-currency-hkd') if price is not None else None,
                                  'sold_out_label_shown': '售完' in a.text_content(),
                                  'source_snapshot': path.stem + '.html', 'source': 'authenticated_catalogue'})
        elif number in {12, 13}:
            pid = '691bf77b836791001431a5b1' if number == 12 else '691bf8caa8021300103cf2c6'
            price = dom.xpath('//*[@data-currency-hkd]')[0]
            catalogue.append({'id': pid, 'sku': public[pid].get('sku'), 'title': dom.xpath('//h1')[0].text_content().strip(),
                              'url': url, 'display_currency': 'GBP', 'display_price': unescape(price.get('data-currency-gbp')),
                              'base_hkd_price': price.get('data-currency-hkd'), 'source_snapshot': path.stem + '.html',
                              'source': 'authenticated_product_page_outside_catalogue'})
    assert len(catalogue) == len({p['id'] for p in catalogue}) == 188
    assert {p['id'] for p in catalogue} == set(public)
    differences = []
    for p in catalogue:
        sale = public[p['id']]['sale_price']
        base = sale if sale and sale.get('label') and sale.get('cents', 0) > 0 else public[p['id']]['price']
        p['public_archive_base_hkd_price'] = base['label']
        p['hkd_price_matches_public'] = cents(p['base_hkd_price']) == base['cents'] if p['base_hkd_price'] else None
        if p['hkd_price_matches_public'] is False:
            differences.append(p['id'])
    assert not differences, differences
    dump(OUT / 'products-authenticated.json', catalogue)
    comparison = {'public_product_count': 188, 'authenticated_catalogue_count': 186, 'additional_voucher_pages': 2,
                  'unique_product_ids_covered': 188, 'catalogue_prices_comparable': 163, 'voucher_prices_comparable': 2,
                  'compared_prices_match': 165, 'price_differences': differences, 'no_price_in_catalogue': 23,
                  'display_currency': 'GBP', 'comparison_currency': 'HKD',
                  'method': 'Read data-currency-hkd from explicitly saved authenticated HTML and compare with public product JSON; no currency conversion estimated.',
                  'detail_sample': {'url': sources[10]['url'], 'observed_text': '若想購買，請聯絡我們。'},
                  'limits': ['Full authenticated descriptions were saved for one sample and two vouchers. Public product descriptions/images remain in the public archive.',
                             'No member-tier discount was observed in the comparable prices. Unpriced or other-tier prices are not inferred.']}
    dump(OUT / 'price-comparison.json', comparison)
    dump(OUT / 'sources.json', sources)
    readme = '''Naturalia Group 會員資料備份
擷取日期：2026-11-16（香港時間）

開啟 index.html 可離線閱讀。此資料夾與公開網站備份分開。

內容：
- member-data.json：帳戶資料、訂閱設定、購物金、優惠券、訊息、訂單、追蹤清單、領券中心及購物車結果。
- products-authenticated.json：登入後商品目錄 186 件，另補 2 款購物金，共 188 個商品 ID。
- price-comparison.json：165 項可比較港幣價格（目錄 163 + 購物金 2）均與公開備份一致；目錄另有 23 件未顯示價格。
- raw/：14 份由瀏覽器儲存檔抽出的靜態頁面；移除程式、隱藏驗證欄位、第三方框架及可提交表單。圖片保留來源連結，原檔另見公開備份。
- text/：14 份文字記錄。
- sources.json：原頁網址、擷取時間、來源 MHTML 雜湊；SHA256SUMS.txt：本備份檔案完整性校驗。

已核對：
購物金 0；可使用、即將開始、已失效優惠券均無記錄；沒有訊息、追蹤商品、可領取優惠券或購物車商品。
訂單頁沒有記錄，但網站明確「僅顯示 2 年內訂單」；更早訂單狀態未知。
選填聯絡電話為空；畫面的英國號碼只是輸入提示，並非已儲存號碼。
沒有顯示已儲存地址或信用卡。姓名、電郵、性別等動態表單值以已核對的 member-data.json 為準。

範圍：
這次取得的是該帳戶可見的顧客會員中心資料，沒有取得商戶管理後台、全店客戶、全部訂單或後台匯出。
已有瀏覽器登入狀態與提供的姓名、電郵、手機一致；沒有輸入或保存提供的密碼。
商品畫面沿用瀏覽器 GBP 顯示，港幣比較值來自儲存頁面的 data-currency-hkd 欄位。
完整公開商品描述、圖片及中英文內容在相鄰 naturalia-group/ 及 naturalia-group-public-data.zip。
僅保存 1 個登入商品詳情抽查及 2 款購物金詳情；其餘登入後商品保存目錄可見資料。未推斷其他會員等級或未顯示價格。
原始 MHTML 留在本機 .cache/naturalia-member/，不包含在可攜式會員 ZIP。
'''
    (OUT / '閱讀說明.txt').write_text(readme, encoding='utf-8')
    rows = ''.join('<tr><td>' + escape(s['label']) + '</td><td><a href="' + s['html'] + '">靜態頁面</a> · <a href="' + s['text'] + '">文字</a></td></tr>' for s in sources)
    summary = ''.join('<tr><td>' + escape(k) + '</td><td>' + escape(v['observed_text']) + '</td></tr>' for k, v in observations['sections'].items())
    product_rows = ''.join('<tr><td>' + escape(p.get('sku') or '') + '</td><td><a href="' + escape(p['url'], quote=True) + '">' + escape(p['title']) + '</a></td><td>' + escape(p['base_hkd_price'] or '未顯示') + '</td><td>' + escape(p['display_price'] or '未顯示') + '</td></tr>' for p in catalogue)
    body = '<h1>Naturalia Group 會員資料備份</h1><p>2026-11-16 · Johnny Lau · 本機離線檔案</p><p><a href="閱讀說明.txt">閱讀說明</a> · <a href="member-data.json">會員 JSON</a> · <a href="products-authenticated.json">商品 JSON</a> · <a href="price-comparison.json">價格核對</a> · <a href="sources.json">來源清單</a></p><p>訂單只可核對最近 2 年；更早紀錄未知。商戶管理後台不在這次取得範圍。</p><h2>會員中心結果</h2><table>' + summary + '</table><h2>14 份頁面記錄</h2><table>' + rows + '</table><h2>188 件商品索引</h2><p>186 件目錄商品及另外 2 款購物金；165 項港幣價格與公開備份一致，23 件未顯示價格。</p><table><thead><tr><th>SKU</th><th>商品</th><th>港幣原價</th><th>瀏覽器顯示價格</th></tr></thead><tbody>' + product_rows + '</tbody></table>'
    (OUT / 'index.html').write_text(document('Naturalia Group 會員資料備份', body), encoding='utf-8')
    files = sorted(p for p in OUT.rglob('*') if p.is_file() and p.name != 'SHA256SUMS.txt')
    (OUT / 'SHA256SUMS.txt').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(OUT).as_posix() + '\n' for p in files), encoding='utf-8')
    for p in OUT.rglob('*'):
        p.chmod(0o700 if p.is_dir() else 0o600)
    OUT.chmod(0o700)
    (CACHE / 'ui-observations.json').chmod(0o600)
    archive = OUT.parent / 'naturalia-group-member-private-data.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT.rglob('*')):
            if p.is_file():
                z.write(p, p.relative_to(OUT.parent))
    archive.chmod(0o600)
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
    print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size,
                      'files': len(files) + 1, 'snapshots': len(sources), 'products': len(catalogue),
                      'compared_prices': 165, 'price_differences': len(differences)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
