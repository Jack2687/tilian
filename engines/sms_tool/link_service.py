#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一提链入口: OA/CS 自动分流, 供 Web 接入。

用法:
    from link_service import extract_ba_link
    result = extract_ba_link(access_token, proxy, billing_country='DE', emit=print)
"""
from __future__ import annotations
import json as _json, time, uuid, secrets, string, re, sys as _sys, os
from pathlib import Path
from typing import Callable, Optional

# paypal 支持的货币国家（账单国=代理出口国）
CURRENCY = {
    'DE': 'EUR', 'GB': 'GBP', 'US': 'USD', 'TH': 'THB', 'VN': 'VND', 'JP': 'JPY',
    'IE': 'EUR', 'FR': 'EUR', 'NL': 'EUR', 'ES': 'EUR', 'IT': 'EUR', 'AT': 'EUR',
    'BE': 'EUR', 'FI': 'EUR', 'PT': 'EUR', 'GR': 'EUR', 'LU': 'EUR', 'SK': 'EUR',
    'SI': 'EUR', 'EE': 'EUR', 'LV': 'EUR', 'LT': 'EUR', 'CY': 'EUR', 'MT': 'EUR',
    'HR': 'EUR', 'AU': 'AUD', 'CA': 'CAD', 'SE': 'SEK', 'NO': 'NOK', 'DK': 'DKK',
    'PL': 'PLN', 'CH': 'CHF', 'SG': 'SGD', 'NZ': 'NZD', 'TR': 'TRY', 'HK': 'HKD',
}
BA_RE = re.compile(r'https://www\.paypal\.com/agreements/approve\?[^\s\"\'<>]+', re.I)

def normalize_proxy(proxy: str) -> str:
    p = (proxy or '').strip()
    if not p: return ''
    # 上游已经统一生成标准代理 URL 时必须原样放行。尤其是 socks5h://，
    # 若继续按 host:port:user:pass 拆分，会被错误重排成含两个 @ 的非法 URL。
    if p.lower().startswith(('http://', 'https://', 'socks5://', 'socks5h://')):
        return p
    # kookeey 格式: host:port:user:pass-国家-sid-时间（user 部分含冒号，不能用 split(':') 拆 4 段）
    # 例: gate-eu.kookeey.info:1000:<用户ID>-<策略用户名>:<策略密码>-DE-xxx-15m
    # 检测最后一个 ':' 后的 pass 段（含 -国家- 标记）
    if 'kookeey.info' in p:
        # 格式: host:port:user:pass  其中 user 不含冒号、pass 含冒号前是完整认证段
        # 更稳妥: 找 'gate-xxx.kookeey.info:' 之后的两个冒号
        m = re.search(r'^(.*kookeey\.info):(\d+):(.+?):(.+)$', p)
        if m:
            host, port, user, passwd = m.group(1), m.group(2), m.group(3), m.group(4)
            return f'http://{user}:{passwd}@{host}:{port}'
    parts = p.split(':')
    if len(parts) == 4:
        return f'http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
    return p

# 提链只使用用户提交的代理池（web 端代理池文本框），不内置任何代理凭据。
# GATES 仅用于从用户代理串解析出口国家提示。

def _proxy_country_hint(proxy: str) -> str:
    """从代理串解析国家标记 (region-XX / -XX- / gate-xx)。解析不到返回 ''。

    优先级:
      1. region-XX / country-XX / cc-XX / geo-XX 显式标记（辣椒格式）
      2. 用户名中 -XX- 独立标记（kookeey 格式 <用户ID>-<策略用户名>-XX-sid-...，gate-eu 等区域网关不算）
      3. gate-xx 网关域名（kookeey gate-gb / gate-de / gate-jp 等国家网关）
    """
    raw = str(proxy or '')
    # 1) 显式 region/country/cc/geo 标记
    m = re.search(r'(?i)(?:region|country|cc|geo)[-_=:/]+([a-z]{2})(?=$|[^a-z0-9])', raw)
    if m and re.fullmatch(r'[A-Z]{2}', m.group(1).upper()):
        return m.group(1).upper()
    # 2) 用户名 -XX- 独立标记（kookeey: user-...-TR-sid-...）
    for m in re.finditer(r'(?<![A-Za-z0-9])([A-Z]{2})(?=[-_@:/]|$)', raw):
        cc = m.group(1).upper()
        if re.fullmatch(r'[A-Z]{2}', cc) and cc in CURRENCY:
            return cc
    # 3) gate-xx 国家网关（忽略 gate-eu / gate-sea 等区域网关）
    m = re.search(r'(?i)gate-([a-z]{2})\.', raw)
    if m:
        cc = m.group(1).upper()
        if cc in CURRENCY and cc not in ('EU', 'SE', 'US'):
            return cc
    return ''

def _load_engine() -> tuple:
    """加载 freepp 协议引擎, 返回 (chain, oaics_proto, billing)."""
    _sys.path.insert(0, os.environ.get('FREEPP_BACKEND', str(Path(__file__).resolve().parent.parent / 'freepp-backend')))
    from core import chain as C
    from core import oaics_proto as op
    from core.billing import billing_for
    return C, op, billing_for

def _resolve_ba(C, proxy, start_url, timeout=15.0, fingerprint=None):
    """多跳 resolve redirect → paypal BA 链（TLS/UA 跟随账号指纹）"""
    from curl_cffi import requests as cr
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    current = str(start_url or '').strip()
    try:
        s = cr.Session(impersonate=str(fp.get('impersonate') or 'chrome136'))
    except Exception:
        s = cr.Session(impersonate='chrome136')
    if fp.get('ua'):
        try:
            s.headers.update({'User-Agent': str(fp['ua'])})
        except Exception:
            pass
    if proxy: s.proxies = {'http': proxy, 'https': proxy}
    try:
        for _ in range(6):
            if not current: break
            if BA_RE.match(current): return current
            if 'paypal.com' in current and 'agreements/approve' in current: return current
            try:
                r = s.get(current, timeout=min(6, max(2, timeout)), allow_redirects=False)
                loc = str(r.headers.get('Location') or '').strip()
                if loc and r.status_code in (301, 302, 303, 307, 308):
                    from urllib.parse import urljoin
                    current = urljoin(current, loc)
                    continue
                m = BA_RE.search(r.text or '')
                if m: return m.group(0)
                return current
            except Exception:
                break
    finally:
        s.close()
    return ''


# ── 自动组合入口: 按代理出口国自动尝试配方矩阵 ───────────────────
# 已实测配方（出口->账单）: TH->DE 压0成功; GB->DE/US->US 有paypal但promo可能失败;
# VN->US/JP->US 出 oaics_; 按代理国选优先组合。
# pay153 账单国家轮换配方: DE→GB→FR→IE→NL (GB/GBP 已实测可压0)
_BILL_ROTATION = [('DE', 'DE', 'EUR'), ('DE', 'GB', 'GBP'), ('DE', 'FR', 'EUR'),
                  ('DE', 'IE', 'EUR'), ('DE', 'NL', 'EUR'), ('TH', 'DE', 'EUR')]
COMBO_MATRIX = {
    'TH': [('TH', 'DE', 'EUR'), ('TH', 'GB', 'GBP'), ('TH', 'FR', 'EUR'),
           ('TH', 'IE', 'EUR'), ('TH', 'NL', 'EUR')],
    'GB': [('GB', 'DE', 'EUR'), ('GB', 'GB', 'GBP'), ('GB', 'FR', 'EUR'),
           ('GB', 'IE', 'EUR'), ('GB', 'NL', 'EUR')],
    'DE': _BILL_ROTATION,
    'US': [('US', 'US', 'USD'), ('US', 'DE', 'EUR'), ('US', 'GB', 'GBP')],
    'VN': [('VN', 'US', 'USD'), ('VN', 'GB', 'GBP'), ('VN', 'DE', 'EUR')],
    'JP': [('JP', 'US', 'USD'), ('JP', 'GB', 'GBP'), ('JP', 'DE', 'EUR')],
    'HK': [('HK', 'US', 'USD'), ('HK', 'DE', 'EUR'), ('HK', 'GB', 'GBP')],
    'IN': [('IN', 'US', 'USD'), ('IN', 'DE', 'EUR'), ('IN', 'GB', 'GBP')],
}
DEFAULT_COMBOS = [('TH', 'DE', 'EUR'), ('GB', 'DE', 'EUR'), ('US', 'US', 'USD'), ('VN', 'US', 'USD')]

def probe_proxy_country(proxy: str) -> str:
    """探测代理出口国（ip-api），失败返回 ''。"""
    import sys as _s
    try:
        _s.path.insert(0, os.environ.get('FREEPP_BACKEND', str(Path(__file__).resolve().parent.parent / 'freepp-backend')))
        from curl_cffi import requests as cr
        r = cr.get('http://ip-api.com/json/?fields=status,countryCode,query',
                   timeout=8, proxy=normalize_proxy(proxy) or None, impersonate='chrome')
        d = r.json() or {}
        return str(d.get('countryCode') or '').upper() if d.get('status') == 'success' else ''
    except Exception:
        return ''

def extract_ba_link_auto(
    access_token: str,
    proxy: str = '',
    prefer_country: str = '',
    payment_proxy: str = '',
    emit: Optional[Callable[[str, str], None]] = None,
    timeout: float = 30.0,
    fingerprint: Optional[dict] = None,
) -> dict:
    """自动组合提链：账单国家 = 代理池解析出的出口国家（代理写什么就用什么），
    不写死 DE。可用 prefer_country(web 附加参数 country) 显式覆盖。
    返回带 amount(压0后) + order_type(oa/cs) 的完整结果。"""
    log = emit or (lambda stage, msg: None)
    t0 = time.monotonic()
    px = normalize_proxy(proxy)
    # 用户写什么就用什么: 不探测、不撞矩阵。
    # 建单 = 主池 px(用户池1); 出口/账单侧 = 第二池 payment_proxy(用户池2)。
    # 账单国家优先取 prefer_country，否则从主代理池解析出口国家（代理写什么就用什么）。
    bill_cc = (prefer_country or '').upper()
    if not bill_cc:
        bill_cc = _proxy_country_hint(proxy) or _proxy_country_hint(payment_proxy) or ''
    if bill_cc not in CURRENCY:
        log('config', f'账单国家 {bill_cc or "(空)"} 不在支持列表 {sorted(CURRENCY.keys())}，'
                      f'请在附加参数指定 country（代理出口国家）')
        return {'ok': False, 'error': f'账单国家 {bill_cc or "(空)"} 不支持，请在附加参数指定 country '
                                      f'（与代理出口一致，如 {"DE"}）；代理={str(proxy)[:50]}',
                'reason_code': 'country_unsupported',
                'stages': [], 'elapsed': round(time.monotonic() - t0, 1)}
    exit_cc = _proxy_country_hint(payment_proxy) or bill_cc
    cur = CURRENCY.get(bill_cc, 'EUR')
    log('config', f'[按用户代理] 建单={px[:30]}... 出口={exit_cc} 账单={bill_cc}/{cur} exit_px={str(payment_proxy)[:36] or "同主池"}')
    try:
        res = extract_ba_link(access_token, px, bill_cc, chain='auto', emit=emit,
                              timeout=timeout, exit_px=payment_proxy or '',
                              fingerprint=fingerprint)
    except Exception as e:
        res = {'ok': False, 'error': f'{type(e).__name__}: {e}', 'reason_code': 'engine_error'}
    if res.get('ok'):
        res['amount'] = 0  # 压0后金额
        res['order_type'] = res.get('chain') or res.get('session_type') or 'cs'
        res['combo'] = f'{exit_cc}->{bill_cc}/{cur}'
        res['exit_country'] = exit_cc
    else:
        res['amount'] = 0
        res['order_type'] = res.get('chain') or res.get('session_type') or 'unknown'
        res['session_type'] = res.get('session_type') or ''
        res['combo'] = res.get('combo') or f'{exit_cc}->{bill_cc}/{cur}'
        res['exit_country'] = exit_cc
    res['elapsed'] = round(time.monotonic() - t0, 1)
    return res


def extract_ba_link(
    access_token: str,
    proxy: str = '',
    billing_country: str = 'DE',
    promo_proxy: str = '',
    chain: str = 'auto',
    emit: Optional[Callable[[str, str], None]] = None,
    timeout: float = 30.0,
    exit_px: str = '',
    fingerprint: Optional[dict] = None,
) -> dict:
    """统一提链入口。chain: auto(建单判定) / oa / cs。"""
    log = emit or (lambda stage, msg: None)
    t0 = time.monotonic()
    stages: list[dict] = []
    def stage(name: str, status: str, detail: str = '', cc: str = ''):
        stages.append({'stage': name, 'status': status, 'detail': detail,
                       'country': cc, 'elapsed': round(time.monotonic() - t0, 1)})
        log(name, f'[{status}] {detail}')
    # 加载引擎
    try:
        C, op, billing_for = _load_engine()
    except Exception as e:
        return {'ok': False, 'error': f'引擎加载失败: {e}', 'reason_code': 'engine_load_failed',
                'stages': stages, 'elapsed': round(time.monotonic() - t0, 1)}
    bill_cc = (billing_country or 'DE').upper()
    cur = CURRENCY.get(bill_cc, 'EUR')
    px = normalize_proxy(proxy)
    # 账号指纹档案的 device_id：同一账号+同建单国家永久稳定，
    # 建单→促销→approve→poll 全程身份一致（禁止中途重新生成）
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    device_id = str(fp.get('device_id') or '').strip() or str(uuid.uuid4())
    if fp:
        stage('fingerprint', 'ok',
              f"账号指纹 {fp.get('kernel_label') or fp.get('kernel') or 'chrome'}/{device_id[:8]} "
              f"TLS={fp.get('impersonate') or '?'} 语言={fp.get('oai_language') or '?'} "
              f"时区={fp.get('timezone') or '?'} 建单国={fp.get('country') or bill_cc}")
    # 建单探测
    try:
        r = C.stage_checkout_live(px, access_token, '', bill_cc, cur, 'paypal',
                                  promo_inline=False, ui_mode='custom', device_id=device_id,
                                  fingerprint=fp or None)
    except Exception as e:
        return {'ok': False, 'error': f'建单异常: {e}', 'reason_code': 'network_error',
                'stages': stages, 'elapsed': round(time.monotonic() - t0, 1)}
    if r.get('status') == 429:
        stage('checkout', 'fail', '建单限流 429')
        return {'ok': False, 'error': '建单限流 429', 'reason_code': 'checkout_rate_limited',
                'stages': stages, 'elapsed': round(time.monotonic() - t0, 1)}
    if not r.get('ok'):
        stage('checkout', 'fail', f"HTTP {r.get('status')}: {str(r.get('detail'))[:100]}")
        rc = 'checkout_billing_mismatch' if r.get('status') == 400 else 'checkout_failed'
        return {'ok': False, 'error': str(r.get('detail'))[:200], 'reason_code': rc,
                'stages': stages, 'elapsed': round(time.monotonic() - t0, 1)}
    cs = r['checkout_session_id']; pk = r['publishable_key']
    entity = r.get('processor_entity') or ('openai_llc' if bill_cc == 'US' else 'openai_ie')
    s_type = 'oaics' if str(cs).startswith('oaics_') else ('cs' if str(cs).startswith('cs_live_') else 'unknown')
    stage('checkout', 'ok', f'{s_type} 会话 {cs[:24]}')
    # 分流
    if s_type == 'oaics' and chain in ('auto', 'oa'):
        # ══ 交接文档 P0: OA 只允许一个地方负责建单 ══
        # 直接复用上方探测建出的 cs（不二次建单），走 freepp 全套
        # （同一 device_id + cookie_jar 上下文贯穿 update/elements/token/confirm）
        res = _run_oaics(C, op, billing_for, px, access_token, cs, pk, entity,
                         bill_cc, cur, device_id, stage, timeout, fingerprint=fp or None)
    elif s_type == 'cs' and chain in ('auto', 'cs'):
        res = _run_cs(C, px, access_token, cs, pk, entity, bill_cc, cur, device_id, stage, timeout,
                      fingerprint=fp or None)
    else:
        return {'ok': False, 'error': f'会话类型 {s_type} 与链 {chain} 不匹配',
                'reason_code': 'chain_mismatch', 'stages': stages,
                'elapsed': round(time.monotonic() - t0, 1)}
    res.update({'session_id': cs, 'session_type': s_type,
                'billing_country': bill_cc, 'currency': cur,
                'stages': stages, 'elapsed': round(time.monotonic() - t0, 1)})
    return res

def _run_oaics(C, op, billing_for, px, at, cs, pk, entity, bill_cc, cur, device_id,
               stage, timeout, fingerprint=None):
    b = billing_for(bill_cc)
    billing = {
        'name': b.get('name') or 'John Doe',
        'email': f'buyer{uuid.uuid4().hex[:8]}@gmail.com',
        'address': {'country': b.get('country', bill_cc), 'line1': b.get('line1', ''),
                    'line2': b.get('line2', ''), 'city': b.get('city', ''),
                    'state': b.get('state', ''), 'postal_code': b.get('postal_code', '')},
    }
    try:
        upd = C.stage_update_live(px, at, '', cs, entity, bill_cc, cur, 'paypal',
                                   device_id=device_id, fingerprint=fingerprint)
        if not upd.get('ok'):
            stage('promo', 'fail', f"update HTTP {upd.get('status')}")
            return {'ok': False, 'error': '促销注入失败', 'reason_code': 'promo_not_applied'}
        stage('promo', 'ok', 'promo 注入成功')
    except Exception as e:
        stage('promo', 'fail', str(e)[:80])
        return {'ok': False, 'error': f'促销注入异常: {e}', 'reason_code': 'network_error'}
    try:
        state = op.fetch_oaics_checkout_state(px, at, '', cs, entity, country=bill_cc, device_id=device_id,
                                              fingerprint=fingerprint)
        methods = op.oaics_payment_method_types(state)
        if 'paypal' not in methods:
            stage('state', 'fail', f'无 paypal: {methods}')
            return {'ok': False, 'error': f'无 paypal: {methods}', 'reason_code': 'no_paypal'}
        cuss = op._oaics_find_string(state, ('customer_session_client_secret', 'customerSessionClientSecret'))
        if not cuss:
            stage('state', 'fail', '无 customer_session_client_secret')
            return {'ok': False, 'error': '无 customer_session_client_secret', 'reason_code': 'no_secret'}
        zero_ok = False
        for attempt in range(5):
            try:
                op.verify_oaics_zero_snapshot(state, session_id=cs, currency=cur.lower())
                zero_ok = True; break
            except op.OaicsPromoNotApplied:
                time.sleep(1.5)
                state = op.fetch_oaics_checkout_state(px, at, '', cs, entity, country=bill_cc, device_id=device_id,
                                                      fingerprint=fingerprint)
        if not zero_ok:
            stage('promo', 'fail', '压0失败(无优惠资格)')
            return {'ok': False, 'error': 'promo 压0失败，账号无优惠资格', 'reason_code': 'promo_not_applied'}
        stage('promo', 'ok', f'0元确认 paypal={methods}')
        op.submit_oaics_checkout_taxes(px, at, '', cs, entity, billing=billing,
                                       country=bill_cc, currency=cur, device_id=device_id,
                                       fingerprint=fingerprint)
        stage('taxes', 'ok')
    except Exception as e:
        stage('state', 'fail', str(e)[:80])
        return {'ok': False, 'error': f'OAICS state: {e}', 'reason_code': 'network_error'}
    try:
        elements = op.create_oaics_elements_session(px, pk, cuss, country=bill_cc,
                                                    currency=cur, methods=['paypal', 'link', 'card'],
                                                    fingerprint=fingerprint)
        ctoken = op.create_oaics_paypal_confirmation_token(px, pk, elements, billing=billing, currency=cur,
                                                           fingerprint=fingerprint)
        stage('token', 'ok', f'ctoken={str(ctoken)[:12]}...')
    except Exception as e:
        stage('token', 'fail', str(e)[:80])
        return {'ok': False, 'error': f'ctoken: {e}', 'reason_code': 'token_failed'}
    try:
        app_confirm = op.confirm_oaics_standard_paypal(
            px, at, '', cs, entity, ctoken, country=bill_cc, device_id=device_id,
            fingerprint=fingerprint)
    except op.OaicsConfirmBlocked as e:
        stage('confirm', 'fail', 'confirm blocked（账号风控）')
        return {'ok': False, 'error': 'OAICS confirm blocked', 'reason_code': 'confirm_blocked'}
    except Exception as e:
        stage('confirm', 'fail', str(e)[:80])
        return {'ok': False, 'error': f'confirm: {e}', 'reason_code': 'confirm_failed'}
    from core.link_helpers import extract_redirect_url
    redirect_url = extract_redirect_url(app_confirm)
    if not redirect_url:
        try:
            intent = op.confirm_oaics_paypal_intent(px, ctoken, app_confirm, elements,
                                                    fingerprint=fingerprint)
            redirect_url = extract_redirect_url(intent)
        except Exception:
            redirect_url = ''
    if not redirect_url:
        stage('confirm', 'fail', '无 redirect')
        return {'ok': False, 'error': 'confirm 成功但无 redirect', 'reason_code': 'no_redirect'}
    ba = _resolve_ba(C, px, redirect_url, timeout, fingerprint=fingerprint)
    if ba and 'paypal.com' in ba:
        stage('resolve', 'ok', 'BA 链提取成功')
        return {'ok': True, 'ba_url': ba, 'chain': 'oaics', 'stripe_redirect_url': redirect_url}
    stage('resolve', 'fail', f'未提取到 BA: {redirect_url[:60]}')
    return {'ok': False, 'error': '未提取到 BA 链', 'reason_code': 'resolve_failed'}

def _run_cs(C, px, at, cs, pk, entity, bill_cc, cur, device_id, stage, timeout, exit_px='',
             fingerprint=None):
    """CS 链【直接创建 0 元单】正确流程（杜绝假0元）:
    建单(外部已建) -> init(确认 paypal) -> 【confirm 前】立即 promo 压0
    -> pay153 全流程(创建 PM -> confirm(金额0, Stripe生成0元SetupIntent) -> approve
    -> poll -> resolve BA 链) -> 强校验 PaymentIntent/amount 必须为 0。

    与旧流程的本质区别:
    - 旧(废弃): 非零 confirm(2300 PaymentIntent) -> 非零 approve -> post-approve 压0
      => Stripe intent 仍是 2300 原价, 是"假0元"链接(用户授权会扣原价或不到账)
    - 新: confirm 前金额已为 0 => Stripe 生成 SetupIntent(0元) => 链接真0元
    - 0元 approve 被风控 blocked 是【正常账号级现象】(用户确认可接受),
      不会产出假0元链接。
    """
    # 1) init: 确认 paypal 在
    try:
        ir = C.stage_init_live(px, pk, cs, 'paypal', fingerprint=fingerprint)
        init = ir.get('init') or {}
        m0 = init.get('payment_method_types') or []
        if 'paypal' not in m0:
            stage('init', 'fail', f'无 paypal: {m0}')
            return {'ok': False, 'error': f'无 paypal: {m0}', 'reason_code': 'no_paypal'}
        amount0 = (init.get('invoice') or {}).get('amount_due')
        stage('init', 'ok', f'init ok amount={amount0} pm={m0}')
    except Exception as e:
        stage('init', 'fail', str(e)[:80])
        return {'ok': False, 'error': f'CS init: {e}', 'reason_code': 'network_error'}

    # 2) pay153 全流程（对齐 pay153 手动提链: 同一会话+同一device_id 贯穿 update->approve）
    try:
        import sys as _sys3
        _sys3.path.insert(0, os.environ.get('PAY153_BACKEND', str(Path(__file__).resolve().parent.parent / 'pay153-checkout-link-main')))
        import stripe_checkout as sc
    except Exception as e:
        stage('flow', 'fail', f'pay153 模块加载失败: {e}')
        return {'ok': False, 'error': f'pay153 模块加载失败: {e}', 'reason_code': 'engine_load_failed'}

    prov_px = exit_px or px
    pay_http = None
    chatgpt_http = None
    try:
        b = C.billing_for(bill_cc) if hasattr(C, 'billing_for') else None
        if b is None:
            from core.billing import billing_for as _bf
            b = _bf(bill_cc)
        billing = {
            'name': b.get('name') or 'John Doe',
            'email': f'buyer{uuid.uuid4().hex[:8]}@gmail.com',
            'address': {'country': b.get('country', bill_cc), 'line1': b.get('line1', ''),
                        'line2': b.get('line2', ''), 'city': b.get('city', ''),
                        'state': b.get('state', ''), 'postal_code': b.get('postal_code', '')},
        }
        pay_http = sc.build_http(prov_px, fingerprint=fingerprint)
        chatgpt_http = sc.build_http(prov_px, fingerprint=fingerprint)

        # ══ 关键修复: 统一设备身份（对齐 pay153 手动流程 + 交接文档 P0）══
        # 文档要求: Cookie 的 oai-did 必须 == Header 的 oai-device-id（同一值）
        # pay153: create->update->approve 全程同一 http 会话 + 同一 device_id
        # ⚠ 必须复用建单时的 device_id（函数入参），禁止重新生成——否则建单/approve 设备断裂被风控
        did = device_id  # 统一: cookie oai-did == header oai-device-id
        try:
            chatgpt_http.cookies.set("oai-did", did, domain="chatgpt.com")
        except Exception:
            pass
        # csrf 暖身（对齐 pay153 create_checkout）
        fp_ua = str((fingerprint or {}).get('ua') or sc.CHROME_UA)
        fp_lang = str((fingerprint or {}).get('oai_language') or 'zh-CN')
        fp_accept = str((fingerprint or {}).get('accept_language') or 'zh-CN,zh;q=0.9,en;q=0.8')
        fp_session = str((fingerprint or {}).get('oai_session_id') or '').strip() or device_id
        try:
            chatgpt_http.get("https://chatgpt.com/api/auth/csrf",
                             headers={"User-Agent": fp_ua,
                                      "Accept": "application/json,text/plain,*/*"},
                             timeout=20)
        except Exception:
            pass
        # 会话级头（对齐 pay153；UA/语言跟随账号指纹档案）
        try:
            chatgpt_http.headers.update({
                "User-Agent": fp_ua,
                "Accept-Language": fp_accept,
                "OAI-Language": fp_lang,
                "OAI-Device-Id": device_id,
                "OAI-Session-Id": fp_session,
                "Origin": "https://chatgpt.com",
            })
        except Exception:
            pass

        def _flog(m):
            txt = str(m)
            stage('flow', 'run', txt[:120])

        # 关键: confirm 之前先套优惠压0（pay153 update_checkout_promo 请求体 + 同一会话）
        def _apply_promo(processor):
            promo_body = {
                "checkout_session_id": cs,
                "processor_entity": processor,
                "plan_name": "chatgptplusplan",
                "price_interval": "month",
                "seat_quantity": 1,
                "discount_code": None,
                "promo_campaign": {"promo_campaign_id": "plus-1-month-free",
                                   "is_coupon_from_query_param": False},
            }
            promo_headers = {
                "Authorization": f"Bearer {at}",
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://chatgpt.com",
                "Referer": f"https://chatgpt.com/checkout/{processor}/{cs}",
                "User-Agent": sc.CHROME_UA,
                "OAI-Language": "zh-CN",
                "OAI-Device-Id": device_id,
                "x-openai-target-path": "/backend-api/payments/checkout/update",
                "x-openai-target-route": "/backend-api/payments/checkout/update",
            }
            try:
                upd = chatgpt_http.post("https://chatgpt.com/backend-api/payments/checkout/update",
                                        json=promo_body, headers=promo_headers, timeout=25)
                ok = 200 <= upd.status_code < 300
                stage('promo', 'ok' if ok else 'fail',
                      f'confirm前压0(统一会话) update HTTP={upd.status_code} ok={ok}')
                return {'status': upd.status_code, 'ok': ok}
            except Exception as e:
                stage('promo', 'fail', f'confirm前压0 EXC: {str(e)[:80]}')
                return {'status': 0, 'ok': False}

        # approve 也走同一会话（oai-did cookie + OAI-Device-Id + csrf 已暖身）
        def _approve_cb(processor):
            app_body = {"checkout_session_id": cs, "processor_entity": processor}
            app_headers = {
                "Authorization": f"Bearer {at}",
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://chatgpt.com",
                "Referer": f"https://chatgpt.com/checkout/{processor}/{cs}",
                "User-Agent": sc.CHROME_UA,
                "OAI-Language": "zh-CN",
                "OAI-Device-Id": device_id,
                "x-openai-target-path": "/backend-api/payments/checkout/approve",
                "x-openai-target-route": "/backend-api/payments/checkout/approve",
            }
            try:
                ar = chatgpt_http.post("https://chatgpt.com/backend-api/payments/checkout/approve",
                                       json=app_body, headers=app_headers, timeout=25)
                text = ar.text or ""
                stage('approve', 'ok' if ar.status_code == 200 else 'fail',
                      f'approve(统一会话) HTTP={ar.status_code} {text[:120]}')
                if ar.status_code != 200:
                    raise RuntimeError(f"ChatGPT approve 失败: {ar.status_code} {text[:200]}")
                payload = {}
                try:
                    payload = ar.json() or {}
                except Exception:
                    pass
                result = str(payload.get("result") or "").lower()
                if result and result != "approved":
                    raise RuntimeError(f"manual_approval approve blocked: result={result}")
                return payload
            except Exception as e:
                raise

        paypal_url, _, ctx = sc.stripe_to_paypal_redirect(
            pay_http, cs,
            billing=billing,
            promotion_billing=None,
            payment_billing=None,
            payment_http=None,
            country=bill_cc,
            chatgpt_http=chatgpt_http,
            access_token=at,
            publishable_key=pk,
            processor_entity=entity,
            approve_callback=_approve_cb,      # 关键: 同一会话 approve
            apply_promo_callback=_apply_promo, # 关键: confirm前压0(同一会话)
            require_zero_due=True,             # 关键: 金额必须为0才继续
            log=_flog,
        )
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if 'blocked' in low:
            stage('approve', 'fail', '0元 approve blocked（账号级风控，正常现象）')
            return {'ok': False, 'error': '0元 approve blocked（账号级风控）', 'reason_code': 'approve_blocked'}
        if '未开放' in low or 'not open' in low or 'no paypal' in low:
            stage('init', 'fail', msg[:100])
            return {'ok': False, 'error': msg[:150], 'reason_code': 'no_paypal'}
        if '优惠' in msg or '金额非 0' in msg or '金额' in msg:
            stage('promo', 'fail', msg[:100])
            return {'ok': False, 'error': f'压0未生效: {msg[:150]}', 'reason_code': 'promo_not_applied'}
        if 'redirect' in low or '未生成' in low:
            stage('poll', 'fail', msg[:100])
            return {'ok': False, 'error': msg[:150], 'reason_code': 'no_redirect'}
        stage('flow', 'fail', f'{type(e).__name__}: {msg[:100]}')
        return {'ok': False, 'error': f'{type(e).__name__}: {msg[:200]}', 'reason_code': 'flow_failed'}
    finally:
        try:
            if pay_http: pay_http.close()
        except Exception: pass
        try:
            if chatgpt_http: chatgpt_http.close()
        except Exception: pass

    if not paypal_url or 'paypal.com' not in str(paypal_url):
        stage('resolve', 'fail', f'未拿到 BA 链: {str(paypal_url)[:80]}')
        return {'ok': False, 'error': f'未拿到 BA 链: {str(paypal_url)[:100]}', 'reason_code': 'resolve_failed'}
    stage('resolve', 'ok', f'BA 链已提取: {str(paypal_url)[:70]}...')

    # ── 强校验：PaymentIntent 必须 0 元（第二渠道 Stripe payment_pages 独立直查）──
    # 旧流程的"假0元"正是 PaymentIntent=2300 残留；这里必须确认 intent 金额为 0
    stripe_due = None
    pi_amount = None
    si_amount = None
    try:
        h3 = sc.build_http(prov_px, fingerprint=fingerprint)
        gr = h3.get(f'https://api.stripe.com/v1/payment_pages/{cs}',
                    params={'key': pk, '_stripe_version': sc.STRIPE_VERSION_FULL},
                    headers=sc._stripe_headers(), timeout=20)
        if gr.status_code == 200:
            gj = gr.json()
            inv3 = gj.get('invoice') or {}
            ts3 = gj.get('total_summary') or {}
            stripe_due = inv3.get('amount_due')
            if stripe_due is None:
                stripe_due = ts3.get('due')
            pi3 = gj.get('payment_intent') or {}
            si3 = gj.get('setup_intent') or {}
            pi_amount = pi3.get('amount')
            si_amount = si3.get('amount')
            stage('promo', 'ok',
                  f'强校验 Stripe直查 invoice.amount_due={inv3.get("amount_due")} '
                  f'PaymentIntent.amount={pi_amount} SetupIntent.amount={si_amount}')
        else:
            stage('promo', 'fail', f'强校验 Stripe直查 HTTP {gr.status_code}')
        h3.close()
    except Exception as e:
        stage('promo', 'fail', f'强校验 Stripe直查 EXC: {str(e)[:80]}')

    # 判定：amount_due 必须 0，且 PaymentIntent 不存在或金额为 0（SetupIntent 允许存在，金额应为 None/0）
    zero_due_ok = (stripe_due == 0)
    pi_zero_ok = (pi_amount is None or pi_amount == 0)
    si_zero_ok = (si_amount is None or si_amount == 0)
    intent_zero_ok = pi_zero_ok and si_zero_ok
    if not (zero_due_ok and intent_zero_ok):
        stage('promo', 'fail',
              f'0元强校验未通过: invoice={stripe_due} PI={pi_amount} SI={si_amount}')
        return {'ok': False,
                'error': f'0元强校验未通过: invoice.amount_due={stripe_due}, '
                         f'PaymentIntent={pi_amount}, SetupIntent={si_amount}',
                'reason_code': 'intent_not_zero',
                'ba_url': str(paypal_url), 'chain': 'cs'}
    res = {'ok': True, 'ba_url': str(paypal_url), 'chain': 'cs',
           'stripe_redirect_url': '', 'amount': 0}
    return res



def extract_gopay_link(
    access_token: str,
    proxy: str = "",
    exit_proxy: str = "",
    country: str = "ID",
    require_zero: bool = True,
    promo_campaign_id: str = "plus-1-month-free",
    checkout_ui_mode: str = "hosted",
    emit: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """GoPay 协议提链（Web 接入）。

    ID 出口建单（支付方式含 gopay），--exit-proxy 可作促销/出口侧代理。
    注意: 当前 OpenAI 区域策略下 ID 出口不授予 plus-1-month-free,
    默认 require_zero=False 输出原价 gopay 授权链接; 促销恢复后传 True 即可校验 0 元。
    """
    t0 = time.monotonic()
    if emit: emit("gopay", "开始 GoPay 协议提链")
    # link_service 常以脚本模式被 Web 调用（python link_service.py），
    # 相对导入会失败，这里注入项目根后使用绝对导入。
    _project_root = str(Path(__file__).resolve().parent.parent)
    if _project_root not in _sys.path:
        _sys.path.insert(0, _project_root)
    from sms_tool.wallet_provider import run_wallet_provider
    from sms_tool.wallet_transport import ChatGPTStripeWalletTransport

    cc = str(country or "ID").strip().upper()
    checkout_proxy = normalize_proxy(proxy)
    side_proxy = normalize_proxy(exit_proxy) or checkout_proxy
    tc = {
        "checkout_proxy": checkout_proxy,
        "promotion_proxy": side_proxy,
        "provider_proxy": side_proxy,
        "approve_proxy": side_proxy,
        "stripe_init_proxy": side_proxy,
        "payment_method_proxy": side_proxy,
        "confirm_proxy": side_proxy,
        "redirect_proxy": side_proxy,
        "gopay_browser_fallback": False,
        "gopay_create_with_promo": True,
        "promotion_attempts": 3,
    }
    if emit: emit("gopay", "建单代理已就绪")
    result = run_wallet_provider(
        "gopay",
        str(access_token or "").strip(),
        ChatGPTStripeWalletTransport(timeout=90),
        link_only=True,
        auth_context={},
        transport_context=tc,
        checkout_country=cc,
        currency="IDR",
        promo_campaign_id=str(promo_campaign_id or "").strip(),
        checkout_ui_mode=str(checkout_ui_mode or "hosted").strip(),
        require_zero=bool(require_zero),
        max_approve_attempts=6,
        max_poll_attempts=25,
        poll_interval_seconds=2.0,
    )
    capability = result.get("capability") or {}
    out = {
        "ok": bool(result.get("ok")),
        "link": str(result.get("url") or ""),
        "error": str(result.get("error") or "") if not result.get("ok") else "",
        "reason_code": str(result.get("error_code") or "") if not result.get("ok") else "",
        "extra": {
            "payment_method": "gopay",
            "link_type": str(result.get("link_type") or ""),
            "amount_minor": capability.get("amount_minor"),
            "currency": capability.get("currency") or "IDR",
            "offer_state": capability.get("offer_state") or "",
            "promotion_applied": result.get("promotion_applied"),
            "promotion_error": str(result.get("promotion_error") or ""),
            "checkout_session_type": str(result.get("checkout_session_type") or ""),
            "checkout_session_id_hint": str(result.get("checkout_session_id_hint") or ""),
            "checkout_ui_mode": str(result.get("checkout_ui_mode") or ""),
            "error_stage": str(result.get("error_stage") or ""),
            "retryable": bool(result.get("retryable")),
            "elapsed_seconds": round(time.monotonic() - t0, 1),
        },
    }
    if isinstance(result.get("checkout_eligibility"), dict):
        out["extra"]["checkout_eligibility"] = result["checkout_eligibility"]
    return out

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--at', required=True)
    ap.add_argument('--proxy', default='')
    ap.add_argument('--country', default='DE')
    ap.add_argument('--chain', default='auto')
    ap.add_argument('--auto', action='store_true', help='按代理出口国自动组合尝试')
    ap.add_argument('--exit-proxy', default='', help='出口/账单侧代理（第二代理池）')
    ap.add_argument('--json', default='')
    ap.add_argument('--mode', default='paypal', choices=['paypal', 'gopay'])
    ap.add_argument('--require-zero', action='store_true', help='gopay: 要求 0 元（默认已开启）')
    ap.add_argument('--no-require-zero', action='store_true', help='gopay: 关闭 0 元校验（原价链接）')
    ap.add_argument('--promo-campaign-id', default='plus-1-month-free')
    ap.add_argument('--checkout-ui-mode', default='hosted')
    ap.add_argument('--fingerprint', default='', help='账号指纹档案 JSON（device_id/ua/impersonate/语言/时区，按建单国家分配）')
    args = ap.parse_args()
    _fp = {}
    if str(args.fingerprint or '').strip():
        try:
            _fp = _json.loads(str(args.fingerprint).strip())
            _fp = _fp if isinstance(_fp, dict) else {}
        except Exception:
            print('[fingerprint] [fail] --fingerprint 不是合法 JSON，回退内置默认指纹', flush=True)
            _fp = {}
    def emit(stage, msg):
        print(f'[{stage}] {msg}', flush=True)
    if args.mode == 'gopay':
        result = extract_gopay_link(
            args.at, proxy=args.proxy, exit_proxy=args.exit_proxy,
            country=args.country, require_zero=not args.no_require_zero,
            promo_campaign_id=args.promo_campaign_id,
            checkout_ui_mode=args.checkout_ui_mode, emit=emit,
        )
    elif args.auto:
        result = extract_ba_link_auto(args.at, args.proxy, prefer_country=args.country, payment_proxy=args.exit_proxy, emit=emit, fingerprint=_fp or None)
    else:
        result = extract_ba_link(args.at, args.proxy, args.country, chain=args.chain, emit=emit, fingerprint=_fp or None)
    print(_json.dumps(result, ensure_ascii=False, separators=(',', ':'), default=str))
    if args.json:
        Path(args.json).write_text(_json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    _sys.exit(0 if result.get('ok') else 1)
