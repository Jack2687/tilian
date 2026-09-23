#!/usr/bin/env python3
"""MoMo OAICS/Stripe 提链 CLI。

按 MoMo HAR 实现完整链路：
  custom checkout -> taxes/state -> Elements Session ->
  confirmation_token(type=momo) -> checkout/confirm -> PaymentIntent redirect
  -> pm-redirects -> payment.momo.vn/v2/gateway/pay。

HAR 实测（www.google.com_2026_08_28_23_33_10.har）关键点：
  - payment_method_types 直接含 momo；custom_payment_methods=[] 是正常现象；
  - 建单后必须 POST /checkout/taxes 提交越南账单，否则 confirm 会因账单缺失被拒；
  - confirm 需携带 oai-web-deployment-attestation + x-oai-is-client-observation
    与建单一致的 openai-sentinel-token / chatgpt-account-id。
"""
from __future__ import annotations
import argparse, json, sys, time, uuid, os, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "engines" / "freepp-backend"
MOMO_ENGINE = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT / "gopay_engine"))
sys.path.insert(0, str(MOMO_ENGINE))
from core import oaics_proto as op
from core.chain import (
    stage_checkout_live, stage_init_live, stage_update_live, stage_payment_method_live,
    stage_confirm_live, stage_approve_live, stage_poll_live, build_ctx, verify_zero,
)
from core.link_helpers import extract_redirect_url, follow_gateway_redirect
from core.billing import billing_for

try:
    from vn_billing import generate_vn_billing, probe_exit_city
except Exception:
    generate_vn_billing = None
    probe_exit_city = None

VN_BILLING_POOL = [
    {'city': 'Hanoi', 'state': 'Ha Noi', 'postal_code': '100000', 'line1': '12 Trang Tien'},
    {'city': 'Ho Chi Minh City', 'state': 'Ho Chi Minh', 'postal_code': '700000', 'line1': '25 Nguyen Hue'},
    {'city': 'Da Nang', 'state': 'Da Nang', 'postal_code': '550000', 'line1': '18 Bach Dang'},
    {'city': 'Hai Phong', 'state': 'Hai Phong', 'postal_code': '180000', 'line1': '9 Dien Bien Phu'},
    {'city': 'Can Tho', 'state': 'Can Tho', 'postal_code': '900000', 'line1': '31 Hoa Binh'},
    {'city': 'Nha Trang', 'state': 'Khanh Hoa', 'postal_code': '650000', 'line1': '7 Tran Phu'},
    {'city': 'Hue', 'state': 'Thua Thien Hue', 'postal_code': '530000', 'line1': '16 Le Loi'},
    {'city': 'Vung Tau', 'state': 'Ba Ria-Vung Tau', 'postal_code': '790000', 'line1': '22 Ha Long'},
]

def resolve_vn_billing(state, fp, device_id, proxy=''):
    """Resolve stable VN billing data, preferring checkout/account data.

    优先级：checkout_state 已填地址 > MOMO_BILLING_ADDRESS 注入 > 真实街道库
    (generate_vn_billing，按代理出口城市同城优先，账号 seed 稳定)。
    """
    checkout = state.get('checkout_state', {}) if isinstance(state, dict) else {}
    current = checkout.get('billingAddress') if isinstance(checkout, dict) else None
    if isinstance(current, dict) and isinstance(current.get('address'), dict):
        addr = current['address']
        if addr.get('line1') and addr.get('city') and addr.get('postal_code'):
            return {'name': current.get('name') or '', 'email': state.get('email') or '', 'address': addr, '_source': 'oaics_checkout_state'}
    raw = str(os.environ.get('MOMO_BILLING_ADDRESS') or '').strip()
    if raw:
        try:
            injected = json.loads(raw)
            addr = injected.get('address') if isinstance(injected, dict) else None
            if isinstance(addr, dict) and addr.get('line1') and addr.get('city') and addr.get('postal_code'):
                return injected
        except Exception:
            pass
    email = str((fp or {}).get('account_email') or (state or {}).get('email') or '')
    # 优先真实街道库（同城 + 账号稳定），失败回退共享池
    if generate_vn_billing is not None:
        geo_city = ''
        if proxy and probe_exit_city is not None:
            try:
                geo_city = probe_exit_city(proxy, timeout=min(6.0, 5.0))
            except Exception:
                geo_city = ''
        try:
            billing = generate_vn_billing(email, country='VN', currency='VND',
                                          seed=email or device_id, geo_city=geo_city)
            if billing.get('address', {}).get('line1'):
                return billing
        except Exception:
            pass
    seed_hint = hashlib.sha256((email or device_id).encode('utf-8')).hexdigest()[:24]
    template = {**billing_for('VN'), **VN_BILLING_POOL[int(seed_hint[:8], 16) % len(VN_BILLING_POOL)]}
    return {'name': template.get('name') or 'Minh Nguyen', 'email': email,
            'address': {'country': 'VN', 'line1': template.get('line1') or '1 Hanoi Main Street',
                        'city': template.get('city') or 'Hanoi', 'state': template.get('state') or 'Hanoi',
                        'postal_code': template.get('postal_code') or '100000'},
            '_source': 'shared_vn_billing',
            'seed_hint': seed_hint}

def emit(stage, status, msg):
    print(f"[{stage}] [{status}] {msg}", flush=True)

def mint_checkout_sentinel(proxy, fp, device_id):
    """Reuse the GoPay sentinel implementation for MoMo checkout risk context."""
    try:
        from sentinel_token import mint_sentinel_sync, sentinel_header_value
        cookie = f"oai-did={device_id}"
        token = mint_sentinel_sync(
            flow='chatgpt_checkout', device_id=device_id,
            user_agent=str((fp or {}).get('ua') or ''), proxy=proxy,
            cores=int((fp or {}).get('hardware_concurrency') or 16),
            page_url='https://chatgpt.com/?promo_campaign=plus-1-month-free',
            language=str((fp or {}).get('oai_language') or 'vi-VN'),
            timezone=str((fp or {}).get('timezone') or 'Asia/Ho_Chi_Minh'),
            cookie_header=cookie, timeout_s=60, emit=lambda *_: None)
        value = sentinel_header_value(token)
        return {'openai-sentinel-token': value} if value else {}
    except Exception as exc:
        emit('sentinel', 'warn', f'哨兵令牌生成失败，继续请求：{type(exc).__name__}')
        return {}

def _merge_taxes_state(state, taxes):
    """把 taxes 响应里的 checkout_state/checkout_session 合并进最新状态快照。"""
    merged = dict(state) if isinstance(state, dict) else {}
    if not isinstance(taxes, dict):
        return merged
    tc_state = taxes.get('checkout_state')
    if isinstance(tc_state, dict):
        merged['checkout_state'] = tc_state
    for key in ('checkout_session', 'amount_total', 'amount_subtotal', 'currency'):
        if key in taxes:
            merged[key] = taxes[key]
    return merged

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--at', required=True); ap.add_argument('--proxy', default=''); ap.add_argument('--exit-proxy', default='')
    ap.add_argument('--country', default='VN'); ap.add_argument('--currency', default='VND')
    ap.add_argument('--promo-campaign-id', default='plus-1-month-free')
    ap.add_argument('--require-zero', action='store_true'); ap.add_argument('--no-require-zero', action='store_true')
    ap.add_argument('--force-payment-method', action='store_true', help='payment_method_types 无 momo 时仍强制提交 momo')
    ap.add_argument('--force-zero', action='store_true', help='未确认 0 元时仍继续 confirm/start')
    ap.add_argument('--checkout-ui-mode', default='auto', help='auto=自动探测；custom=oaics 会话；hosted=cs_live_ 会话')
    ap.add_argument('--session-token', default=''); ap.add_argument('--fingerprint', default='')
    ap.add_argument('--json', default='')
    a=ap.parse_args(); country=a.country.upper(); currency=a.currency.upper(); require_zero=not a.no_require_zero
    fp=json.loads(a.fingerprint) if a.fingerprint else {'device_id':str(uuid.uuid4()),'locale':'vi-VN','timezone':'Asia/Ho_Chi_Minh','impersonate':'chrome136','ua':'','accept_language':'vi-VN,vi;q=0.9'}
    device_id=str(fp.get('device_id') or uuid.uuid4())
    at=a.at.strip(); st=a.session_token.strip(); t0=time.monotonic()
    cookie_jar = {}  # 链级 cookie 罐：建单 -> state/taxes -> confirm 会话一致
    ui_mode = (a.checkout_ui_mode or 'auto').strip().lower()
    try:
        if ui_mode == 'hosted':
            return _run_momo_hosted(a, at, st, country, currency, device_id, fp, t0, require_zero)
        if ui_mode == 'custom':
            co, risk_headers = _momo_checkout(a, at, st, country, currency, device_id, fp, 'custom',
                                              bool(a.promo_campaign_id), cookie_jar)
            return _run_momo_oaics(a, at, st, country, currency, device_id, fp, t0, require_zero,
                                   co, risk_headers, cookie_jar)

        # ── auto：对齐 GoPay/GCash —— 按建单返回的 cs 前缀自动分流 ──
        # custom 建单：cs_live_ → hosted(payment_pages) 流程；oaics_ → oaics(Elements) 流程。
        co, risk_headers = _momo_checkout(a, at, st, country, currency, device_id, fp, 'custom',
                                          bool(a.promo_campaign_id), cookie_jar)
        cs = str(co.get('checkout_session_id') or '')
        if cs.startswith('cs_live_'):
            emit('checkout', 'info', '检测到 cs_live_ 会话，走 hosted(payment_pages) 提链路径')
            return _run_momo_hosted(a, at, st, country, currency, device_id, fp, t0, require_zero,
                                    co=co, risk_headers=risk_headers)
        if cs.startswith('oaics_'):
            emit('checkout', 'info', '检测到 oaics 会话，走 oaics(Elements) 提链路径')
            return _run_momo_oaics(a, at, st, country, currency, device_id, fp, t0, require_zero,
                                   co, risk_headers, cookie_jar)
        raise RuntimeError(f"未知会话类型: {cs[:24]}")
    except Exception as e:
        cls = type(e).__name__
        msg = str(e)
        low = msg.lower()
        retryable = True
        reason_code = 'momo_failed'
        if cls == 'OaicsAuthError' or '401' in msg or 'access_token' in low or 'invalid or expired' in low:
            retryable = False
            reason_code = 'access_token_invalid_or_expired'
        elif cls == 'OaicsPromoNotApplied' or ('金额不是' in msg) or ('promo' in low and ('not applied' in low or '未' in msg)):
            # 优惠资格未生效（金额非 0 / 促销未应用）：按业务要求直接终止该账号提链。
            # 原先允许重试是因为换建单代理出口有时能拿到 0 元资格；现不再换代理重试，
            # 避免在无 0 元资格的账号上反复消耗代理与时间。
            retryable = False
            reason_code = 'promo_not_applied'
        elif '未返回 momo' in msg:
            # 账号无 MoMo 渠道资格（OAICS payment_method_types 不含 momo）：直接终止。
            retryable = False
            reason_code = 'wallet_method_unavailable_env'
        out = _momo_fail(cls, msg, t0, retryable, reason_code)
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return 1
    return 0


def _momo_checkout(a, at, st, country, currency, device_id, fp, ui_mode, promo_inline, cookie_jar):
    """建单（custom 或 hosted），返回 (co, risk_headers)。"""
    emit('checkout', 'run', f'MoMo {ui_mode} 建单')
    risk_headers = mint_checkout_sentinel(a.proxy, fp, device_id)
    if risk_headers:
        emit('sentinel', 'ok', 'MoMo 建单已携带 openai-sentinel-token')
    co = stage_checkout_live(a.proxy, at, st, country, currency, 'momo',
                             promo_inline=promo_inline, ui_mode=ui_mode,
                             sentinel_headers=risk_headers, device_id=device_id,
                             fingerprint=fp, cookie_jar=cookie_jar, check_card_proxy=False)
    if not co.get('ok'):
        raise RuntimeError(f"checkout HTTP {co.get('status')}: {co.get('detail')}")
    return co, risk_headers


def _run_momo_oaics(a, at, st, country, currency, device_id, fp, t0, require_zero,
                    co, risk_headers, cookie_jar):
    """MoMo oaics 路径（已建单）：taxes -> Elements -> confirmation_token -> confirm。"""
    try:
        cs=str(co.get('checkout_session_id') or ''); entity=str(co.get('processor_entity') or 'openai_llc'); pk=str(co.get('publishable_key') or '')
        state = co.get('raw') if isinstance(co.get('raw'), dict) else {}
        methods=op.oaics_payment_method_types(state)
        emit('state','ok',f"payment_method_types={methods} custom_payment_methods={len(op.oaics_custom_payment_methods(state))}")
        # 对齐 GCash：初始建单响应缺 momo 时，先 fetch_oaics_checkout_state 刷新一次再读，
        # 并合并两份数据以保留 customer_session_client_secret 等密钥。
        if 'momo' not in methods and not a.force_payment_method:
            try:
                refreshed = op.fetch_oaics_checkout_state(
                    a.proxy, at, st, cs, entity, country=country, device_id=device_id,
                    cookie_jar=cookie_jar, fingerprint=fp)
                if isinstance(refreshed, dict):
                    state = {**state, **refreshed}
                    methods = op.oaics_payment_method_types(state)
                    emit('state', 'ok', f"OAICS state 已刷新：payment_method_types={methods}")
            except Exception as exc:
                emit('state', 'warn', f"OAICS state 刷新失败：{type(exc).__name__}")
        if 'momo' not in methods:
            if not a.force_payment_method:
                hint = ''
                try:
                    from sms_tool.paypal_proxy import infer_proxy_country, probe_proxy
                    hint = infer_proxy_country(a.proxy)
                    if not hint:
                        pr = probe_proxy(a.proxy, expected_country='VN', timeout=6)
                        hint = str(getattr(pr, 'country_code', '') or '').upper()
                except Exception:
                    pass
                suffix = f"（代理出口={hint or '未知'}，MoMo 需要越南出口 IP）"
                raise RuntimeError(f"OAICS 未返回 momo: {methods}{suffix}")
            emit('state', 'warn', f"payment_method_types 无 momo（={methods}），已按 --force-payment-method 强制提交 momo")
            methods = list(dict.fromkeys([*methods, 'momo']))

        # 部署证明 (oai-web-deployment-attestation) + 会话 cookie，confirm 需要
        try:
            bootstrap = op.bootstrap_oaics_checkout_context(
                a.proxy, at, st, cs, entity, country=country, device_id=device_id,
                cookie_jar=cookie_jar, fingerprint=fp)
            attestation = str(bootstrap.get('attestation') or '')
        except Exception as exc:
            bootstrap = {}; attestation = ''
            emit('attestation', 'warn', f'部署证明获取失败，继续：{type(exc).__name__}')
        confirm_risk = dict(risk_headers)
        if attestation:
            confirm_risk['oai-web-deployment-attestation'] = attestation
            confirm_risk['x-oai-is-client-observation'] = 'true'
            emit('attestation', 'ok', 'MoMo confirm 已携带部署证明 + client-observation')

        billing=resolve_vn_billing(state, fp, device_id, proxy=a.proxy)
        emit('taxes','run',f"提交 VN 账单 city={billing.get('address',{}).get('city','')} source={billing.get('_source','unknown')}")
        taxes=op.submit_oaics_checkout_taxes(a.proxy,at,st,cs,entity,billing=billing,country=country,currency=currency,device_id=device_id,cookie_jar=cookie_jar,fingerprint=fp)
        state=_merge_taxes_state(state, taxes)
        emit('taxes','ok',f"VN 账单已接受 amount_total={taxes.get('amount_total') if isinstance(taxes,dict) else '?'}")

        obs=op.oaics_amount_observations(state); amounts=sorted({v for _,v in obs})
        amount=amounts[0] if len(amounts)==1 else None
        if require_zero and amount != 0:
            if not a.force_zero:
                raise RuntimeError(f"MoMo checkout 金额不是 0: {obs[:8]}")
            emit('amount', 'warn', f"未确认服务端 0 元（amount={amount}），已按 --force-zero 继续 confirm（实际金额以服务端为准）")
        cuss=str(op._oaics_find_string(state,('customer_session_client_secret','customerSessionClientSecret','client_secret')) or '')
        if not pk or not cuss: raise RuntimeError('OAICS 缺少 publishable_key 或 customer_session_client_secret')

        emit('elements','run',f"初始化 Stripe Elements Session amount={amount}")
        elements=op.create_oaics_elements_session(a.proxy,pk,cuss,country=country,currency=currency,methods=methods,fingerprint=fp,amount_minor=amount)
        emit('billing','ok',f"VN billing source={billing.get('_source','unknown')} city={billing.get('address',{}).get('city','')}")
        emit('token','run','创建 MoMo confirmation_token')
        ctoken=op.create_oaics_paypal_confirmation_token(a.proxy,pk,elements,billing=billing,currency=currency,fingerprint=fp,payment_method_type='momo')
        emit('confirm','run','提交 selected_payment_method_type=momo')
        cf=op.confirm_oaics_standard_paypal(a.proxy,at,st,cs,entity,ctoken,country=country,device_id=device_id,fingerprint=fp,payment_method_type='momo',sentinel_headers=confirm_risk,attestation=attestation,cookie_jar=cookie_jar)
        redirect=extract_redirect_url(cf)
        if not redirect:
            ic=op.confirm_oaics_paypal_intent(a.proxy,ctoken,cf,elements,fingerprint=fp); redirect=extract_redirect_url(ic)
        if not redirect: raise RuntimeError('Stripe 未返回 MoMo redirect_to_url')
        emit('redirect','ok','Stripe redirect 已生成，跟随至 MoMo 网关')
        gateway_proxy = a.exit_proxy or a.proxy
        emit('gateway','run',f"使用出口代理={'yes' if a.exit_proxy else 'build-proxy-fallback'}")
        gw=_resolve_momo_gateway(gateway_proxy, a.proxy, redirect, fp)
        final=next((str(value) for value in (gw.get('final_url'), gw.get('hosted_instructions_url'))
                    if 'payment.momo.vn' in str(value or '').lower()), str(redirect))
        if 'payment.momo.vn' not in final.lower(): raise RuntimeError(f'未解析到 MoMo pay URL: {final[:120]}')
        out={'ok':True,'link':final,'extra':{'payment_method':'momo','amount_minor':amount,'currency':currency,'checkout_session_type':'oaics','checkout_session_id_hint':cs[:24],'promotion_applied':amount==0,'link_type':'momo_protocol','elapsed_seconds':round(time.monotonic()-t0,1),'payment_method_types':methods}}
        print(json.dumps(out,ensure_ascii=False),flush=True)
        return 0
    except Exception as e:
        cls = type(e).__name__
        msg = str(e)
        low = msg.lower()
        retryable = True
        reason_code = 'momo_failed'
        if cls == 'OaicsAuthError' or '401' in msg or 'access_token' in low or 'invalid or expired' in low:
            retryable = False
            reason_code = 'access_token_invalid_or_expired'
        elif cls == 'OaicsPromoNotApplied' or ('金额不是' in msg) or ('promo' in low and ('not applied' in low or '未' in msg)):
            # 优惠资格未生效（金额非 0 / 促销未应用）：按业务要求直接终止该账号提链。
            # 原先允许重试是因为换建单代理出口有时能拿到 0 元资格；现不再换代理重试，
            # 避免在无 0 元资格的账号上反复消耗代理与时间。
            retryable = False
            reason_code = 'promo_not_applied'
        elif '未返回 momo' in msg:
            # 账号无 MoMo 渠道资格（OAICS payment_method_types 不含 momo）：直接终止。
            retryable = False
            reason_code = 'wallet_method_unavailable_env'
        out = _momo_fail(cls, msg, t0, retryable, reason_code)
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return 1


def _momo_fail(cls, msg, t0, retryable=True, reason_code='momo_failed'):
    return {'ok': False, 'error': f'{cls}: {msg}', 'reason_code': reason_code,
            'extra': {'error_stage': 'momo', 'retryable': retryable,
                      'elapsed_seconds': round(time.monotonic() - t0, 1)}}


def _resolve_momo_gateway(primary_proxy, fallback_proxy, redirect, fingerprint):
    """Resolve the Stripe handoff, retrying through the checkout proxy."""
    proxies = [str(primary_proxy or '').strip()]
    fallback = str(fallback_proxy or '').strip()
    if fallback and fallback not in proxies:
        proxies.append(fallback)
    last = {}
    for proxy in proxies:
        last = follow_gateway_redirect(proxy, redirect, fingerprint=fingerprint)
        candidates = [last.get('final_url'), last.get('hosted_instructions_url')]
        if any('payment.momo.vn' in str(value or '').lower() for value in candidates):
            return last
    return last


def _run_momo_hosted(a, at, st, country, currency, device_id, fp, t0, require_zero,
                     co=None, risk_headers=None):
    """MoMo CS（cs_live_ / hosted）路径：Stripe payment_pages 流程。

    与 oaics 不同：hosted 建单产生 cs_live_ 会话，走
      checkout(hosted) -> init(确认 momo) -> update(压0) -> PM(momo) -> confirm
      -> approve -> poll -> resolve(payment.momo.vn)
    复用 chain.py 的 7 段 stage 函数（与 paypal/gopay 同栈）。

    若调用方已用 custom 建出 cs_live_ 会话（co 非空），直接复用，不再二次建单。
    """
    from core.chain import stage_checkout_live, stage_init_live, stage_update_live, stage_payment_method_live, \
        stage_confirm_live, stage_approve_live, stage_poll_live, build_ctx, verify_zero

    try:
        if co is None:
            emit('checkout', 'run', 'MoMo CS hosted 建单')
            risk_headers = mint_checkout_sentinel(a.proxy, fp, device_id)
            if risk_headers:
                emit('sentinel', 'ok', 'MoMo 建单已携带 openai-sentinel-token')
            # hosted 模式：checkout 不带 promo，先拿真实渠道；update 段再压 0
            co = stage_checkout_live(a.proxy, at, st, country, currency, 'momo',
                                     promo_inline=False, ui_mode='hosted',
                                     sentinel_headers=risk_headers, device_id=device_id,
                                     fingerprint=fp, cookie_jar={}, check_card_proxy=False)
            if not co.get('ok'):
                raise RuntimeError(f"checkout HTTP {co.get('status')}: {co.get('detail')}")
        else:
            emit('checkout', 'ok', '复用已建 cs_live_ 会话')
        cs = str(co.get('checkout_session_id') or '')
        entity = str(co.get('processor_entity') or 'openai_llc')
        pk = str(co.get('publishable_key') or '')
        if not cs or not pk:
            raise RuntimeError('hosted 建单缺少 checkout_session_id / publishable_key')

        # init：确认 momo 在 payment_method_types
        emit('init', 'run', 'Stripe payment_pages init')
        ir = stage_init_live(a.proxy, pk, cs, 'momo', fingerprint=fp)
        init = ir.get('init') or {}
        verify_zero(init, require_zero=False, channel_check=True, channel='momo')
        emit('init', 'ok', f"init ok pm={init.get('payment_method_types')}")

        # update：注入 promo 压 0（hosted 链路“先渠道后压0”）
        emit('update', 'run', '注入 promo 压 0')
        upd = stage_update_live(a.proxy, at, st, cs, entity, country, currency, 'momo',
                                device_id=device_id, fingerprint=fp)
        if not upd.get('ok'):
            raise RuntimeError(f"update failed: {upd.get('status')}")
        init2 = stage_init_live(a.proxy, pk, cs, 'momo', fingerprint=fp).get('init') or {}
        gate = verify_zero(init2, require_zero=require_zero, channel_check=True, channel='momo')
        emit('update', 'ok', f"压0完成 amount_due={gate['amount_due']}")
        ctx = build_ctx(init2)

        # PM(momo) + confirm
        emit('provider', 'run', '创建 MoMo payment_method')
        pm = stage_payment_method_live(a.proxy, pk, cs, init2, country, ctx, 'momo', fingerprint=fp)
        emit('provider', 'ok', f"pm={str(pm)[:16]}...")
        emit('confirm', 'run', 'Stripe confirm')
        cf = stage_confirm_live(a.proxy, pk, cs, init2, pm, ctx, country, entity,
                                require_zero=require_zero, channel_check=True,
                                channel='momo', branch='momo', fingerprint=fp)
        redirect = cf.get('redirect', '')
        state = cf.get('confirm_state', '')

        # approve（requires_approval 时）
        if not redirect and state == 'requires_approval':
            emit('approve', 'run', 'ChatGPT checkout approve')
            ap = stage_approve_live(a.proxy, at, st, cs, entity, 'momo', fingerprint=fp)
            if isinstance(ap, dict):
                redirect = ap.get('redirect', '') or redirect
        # poll（无 redirect 时轮询）
        if not redirect:
            emit('poll', 'run', '轮询 payment_pages')
            poll_res = stage_poll_live(a.proxy, pk, cs, None, 'momo', fingerprint=fp)
            redirect = poll_res.get('redirect', '') if isinstance(poll_res, dict) else str(poll_res or '')
        if not redirect:
            raise RuntimeError('no redirect after confirm/approve/poll')

        # resolve：跟随到 payment.momo.vn
        emit('resolve', 'run', '跟随 redirect 到 MoMo 网关')
        gateway_proxy = a.exit_proxy or a.proxy
        gw = _resolve_momo_gateway(gateway_proxy, a.proxy, redirect, fp)
        final = next((str(value) for value in (gw.get('final_url'), gw.get('hosted_instructions_url'))
                      if 'payment.momo.vn' in str(value or '').lower()), str(redirect))
        if 'payment.momo.vn' not in final.lower():
            raise RuntimeError(f'未解析到 MoMo pay URL: {final[:120]}')

        out = {'ok': True, 'link': final,
               'extra': {'payment_method': 'momo', 'amount_minor': gate.get('amount_due', 0),
                         'currency': currency, 'checkout_session_type': 'cs_live',
                         'checkout_session_id_hint': cs[:24], 'promotion_applied': gate.get('amount_due', 0) == 0,
                         'link_type': 'momo_protocol',
                         'elapsed_seconds': round(time.monotonic() - t0, 1),
                         'payment_method_types': init2.get('payment_method_types') or []}}
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return 0
    except Exception as e:
        cls = type(e).__name__
        msg = str(e)
        low = msg.lower()
        retryable = True
        reason_code = 'momo_failed'
        if cls == 'OaicsAuthError' or '401' in msg or 'access_token' in low:
            retryable = False
            reason_code = 'access_token_invalid_or_expired'
        elif 'NonZeroAmount' in cls or ('amount_due' in low and '非 0' in msg):
            # 优惠资格未生效（金额非 0）：按业务要求直接终止该账号提链。
            retryable = False
            reason_code = 'promo_not_applied'
        elif 'ChannelMismatch' in cls or '不含目标渠道' in msg:
            # 账号无 MoMo 渠道资格：直接终止。
            retryable = False
            reason_code = 'wallet_method_unavailable_env'
        out = _momo_fail(cls, msg, t0, retryable, reason_code)
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return 1


if __name__=='__main__': raise SystemExit(main())
