from __future__ import annotations
import base64
import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import random
import re
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlsplit
from curl_cffi import requests as curl_requests
try:
    from upi_mandate import checkout_payment_settings, mandate_matches, server_upi_mandate
except ImportError:  # direct spec loading from an embedding application
    checkout_payment_settings = None
    mandate_matches = None
    server_upi_mandate = None
CHECKOUT_REGION = 'IN'
# All UPI stages must retain the same India egress.  Rewriting the promotion
# preflight to VN made eligibility and Checkout observe different countries.
PROMOTION_REGION = 'IN'
PROVIDER_REGION = 'IN'
APPROVE_REGION = 'IN'
MAX_APPROVE_ATTEMPTS = 6
# The source UPI worker rebuilds the whole Checkout session up to five times.
# Keep that boundary in the Python service as well; the outer Node/Worker retry
# still remains responsible for selecting a new task attempt.
MAX_REBUILD_ATTEMPTS = 5
# 明确的支付设置拒绝作为终态返回，不在同一 Checkout 上再次提交。
# generic_decline 本身不能证明风控拦截或某个请求字段是拒绝原因。
MAX_SETUP_INTENT_RETRIES = 0
AMOUNT_POLICY_MAX = 50000
UPI_TLS_CONFIGURATION_ERROR = 'UPI 本地证书配置错误：请求库无法加载 CA 证书，请检查运行环境（curl 77）'
_UPI_CA_CACHE_LOCK = threading.Lock()
_UPI_CA_CACHE: dict[str, Any] = {}


def _upi_ascii_ca_path(source: str) -> str:
    """Retain the selected trust bundle while avoiding Windows narrow paths.

    Some libcurl TLS backends cannot open a CA filename containing non-ASCII
    characters. Python can read it, so keep an exact copy in a private process
    temporary directory. TemporaryDirectory cleans it up at process exit.
    """
    try:
        pem = Path(source).read_bytes()
        if not pem:
            raise ValueError('empty CA bundle')
        digest = hashlib.sha256(pem).hexdigest()
        with _UPI_CA_CACHE_LOCK:
            directory = _UPI_CA_CACHE.get(digest)
            if directory is None:
                directory = tempfile.TemporaryDirectory(prefix='tilian-ca-')
                if not directory.name.isascii():
                    directory.cleanup()
                    raise ValueError('CA temporary directory must have an ASCII path')
                _UPI_CA_CACHE[digest] = directory
            target = Path(directory.name) / 'cacert.pem'
            if not target.is_file() or target.read_bytes() != pem:
                temporary = target.with_suffix('.tmp')
                temporary.write_bytes(pem)
                temporary.replace(target)
            return str(target)
    except (OSError, ValueError) as exc:
        raise RuntimeError(UPI_TLS_CONFIGURATION_ERROR) from exc


def _new_upi_http_session(impersonate: str) -> Any:
    # Let curl_cffi choose REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE first, retaining
    # enterprise trust configuration. Otherwise use its effective default.
    session = curl_requests.Session(impersonate=impersonate)
    try:
        if os.name == 'nt':
            from curl_cffi.curl import DEFAULT_CACERT
            source = session.verify if isinstance(session.verify, str) else DEFAULT_CACERT
            if not str(source).isascii():
                session.verify = _upi_ascii_ca_path(str(source))
        session.trust_env = False
        return session
    except Exception:
        session.close()
        raise


# UPI 对外错误统一使用中文；原始英文仅用于服务端调试，不直接返回前端。
def localize_upi_error(error: Any) -> str:
    raw = str(error or '').strip()
    if not raw:
        return 'UPI 提链失败：未收到上游返回信息，请稍后重试'
    text = raw.lower()

    if (
        raw.startswith('UPI 本地证书配置错误')
        or re.search(r'curl\s*:?\s*\(77\)', text)
        or 'error adding trust anchors' in text
        or 'error setting certificate verify locations' in text
    ):
        return UPI_TLS_CONFIGURATION_ERROR

    # 已经是业务中文时，去掉括号中的英文上游原文，避免前端继续出现英文。
    # “优惠未应用”和“账号明确无资格”必须分开：前者表示 Checkout
    # 没有把金额压到 0，不能误报成账号没有资格。
    if '优惠未应用' in raw or 'upi promo not applied' in text:
        return 'UPI 优惠未应用：Checkout 返回应付金额未压到 0，请检查优惠上下文或更换代理重试'
    if '优惠压0失败' in raw or '账号无优惠资格' in raw:
        return 'UPI 优惠压0失败：账号无优惠资格'
    if '登录凭证已失效' in raw or '凭证已失效' in raw:
        return 'UPI 账号登录凭证已失效，请重新获取 AT 后再试'
    if '任务已停止' in raw:
        return 'UPI 提链任务已停止'
    if raw.startswith('UPI 账单地址'):
        return raw
    if raw.startswith('UPI Stripe 支付设置失败'):
        return raw

    # Stripe 在 ChatGPT approve 之后可能创建 SetupIntent；如果上游返回
    # setup_attempt_failed/generic_decline，失败发生在支付设置阶段，不能再
    # 退化成“未识别错误”或误判为优惠字段缺失。
    if (
        'checkout_approval_payment_failure_with_payment_error' in text
        or 'setup_attempt_failed' in text
        or 'generic_decline' in text
        or 'upi_payment_setup_declined' in text
        or 'upi custom checkout submission failed' in text
        or 'upi stripe intent failed' in text
        or 'payment_method_provider_decline' in text
    ):
        if any(marker in text for marker in ('risk_blocked', 'status=blocked', 'result=blocked', '风控拦截')):
            return 'UPI Checkout 被上游风控拦截（blocked），当前 Checkout 已终止'
        codes = [code for code in (
            'setup_attempt_failed', 'generic_decline', 'payment_method_provider_decline',
            'processing_error', 'checkout_approval_payment_failure_with_payment_error',
        ) if code in text]
        detail = '；错误码=' + ', '.join(codes) if codes else ''
        return 'UPI Stripe 支付设置失败：当前 Checkout 已终止' + detail

    # 保证中文错误可重复归一化：网络层可能先把异常翻译一次，CLI
    # 捕获后还会再次调用本函数；二次处理必须保留原来的细分类，
    # 不能因为已经是中文就退化成“未识别错误”。
    localized_markers = (
        ('UPI checkout confirm blocked', 'UPI Checkout 确认被上游风控拦截（blocked），请检查账号会话与代理环境'),
        ('UPI checkout confirm rejected', 'UPI Checkout 确认被上游拒绝，请查看脱敏诊断中的 status/code/reason'),
        ('UPI checkout confirm HTTP', 'UPI Checkout 确认请求被上游拒绝，请查看脱敏 HTTP 状态和响应字段'),
        ('代理格式错误', 'UPI 代理格式错误：请使用 host:port:user:password 格式'),
        ('代理连接失败', 'UPI 代理连接失败：代理认证失败或 SOCKS 通道被上游关闭，请更换代理'),
        ('代理网络超时', 'UPI 代理网络超时，请更换代理后重试'),
        ('代理安全连接异常', 'UPI 代理安全连接异常，请更换代理后重试'),
        ('网络请求失败', 'UPI 网络请求失败：当前代理或上游连接不可用，请更换代理后重试'),
        ('风控验证模块不可用', 'UPI 风控验证模块不可用，暂时无法继续提链，请联系管理员'),
        ('风控验证令牌为空', 'UPI 风控验证令牌为空，无法继续提链，请更换环境后重试'),
        ('部署证明缺失', 'UPI 风控上下文生成失败：当前 Firefox 会话未返回完整风控证明，本次任务已终止'),
        ('风控上下文生成失败', 'UPI 风控上下文生成失败：当前 Firefox 会话未返回完整风控证明，本次任务已终止'),
        ('支付前置字段缺失', 'UPI Checkout 支付前置字段缺失：当前账号或会话未返回 Stripe 支付配置，无法继续提链'),
        ('Elements 会话创建失败', 'UPI Stripe Elements 会话创建失败：上游未返回有效会话编号'),
        ('确认令牌创建失败', 'UPI Stripe 确认令牌创建失败：上游未返回有效确认令牌'),
        ('Checkout 确认失败', 'UPI Checkout 确认失败：上游未返回有效 PaymentIntent'),
        ('支付意图未生成支付链接', 'UPI 支付意图未生成支付链接：请更换代理或检查账号支付状态'),
        ('请求协议环境不兼容', 'UPI 请求协议环境不兼容：当前浏览器指纹不受支持，请更换请求环境'),
        ('缺少代理', 'UPI 提链缺少代理，请配置印度支付代理'),
        ('缺少账号登录凭证', 'UPI 缺少账号登录凭证，请重新提交有效 AT'),
        ('Checkout 建单失败', 'UPI Checkout 建单失败：上游未返回有效 Checkout 会话编号'),
        ('未提供印度 UPI', 'UPI Checkout 未提供印度 UPI 支付方式，请检查支付代理出口和账号支付资格'),
        ('优惠应用失败', 'UPI 优惠应用失败：上游未接受优惠申请，请检查优惠代理和账号资格'),
        ('税费信息请求失败', 'UPI 税费信息请求失败：无法完成印度账单校验'),
        ('Stripe 确认令牌请求失败', 'UPI Stripe 确认令牌请求失败，请更换代理后重试'),
        ('Checkout 确认请求失败', 'UPI Checkout 确认请求失败，请更换代理或检查账号状态'),
        ('Stripe 支付确认失败', 'UPI Stripe 支付确认失败，请更换代理或检查账号支付状态'),
        ('支付状态查询失败', 'UPI 支付状态查询失败，请更换代理后重试'),
        ('上游拒绝了本次请求', 'UPI 上游拒绝了本次请求（403），请检查账号状态和代理网络'),
        ('请求过于频繁', 'UPI 请求过于频繁（429），请更换代理或稍后重试'),
        ('上游服务暂时不可用', 'UPI 上游服务暂时不可用，请更换代理或稍后重试'),
    )
    for marker, message in localized_markers:
        if marker in raw:
            return message

    if (
        'this promotion is not available' in text
        or 'promotion is not available' in text
        or 'invalid promotion' in text
        or 'invalid_promotion' in text
        or 'promo not eligible' in text
        or 'promotion not eligible' in text
        or 'promotion update failed' in text and ('403' in text or 'not available' in text)
        or 'upi promo not eligible' in text
        or 'upi promo already redeemed' in text
        or 'coupon' in text and ('ineligible' in text or 'not eligible' in text or 'redeemed' in text)
    ):
        if 'redeemed' in text or 'already' in text:
            return 'UPI 优惠压0失败：该账号已使用过此优惠，不具备再次领取资格'
        return 'UPI 优惠压0失败：账号无优惠资格'

    if (
        'invalidated' in text
        or 'access token' in text
        or 'unauthorized' in text
        or 'authentication token' in text
        or re.search(r'\bhttp\s*401\b', text)
        or re.search(r'\bstatus(?:\s*code)?\s*401\b', text)
    ):
        return 'UPI 账号登录凭证已失效，请重新获取 AT 后再试'

    if 'upi promo amount unavailable' in text:
        return 'UPI 优惠未应用：Checkout 未返回可确认的应付金额，请更换代理重试'
    if (
        'deferred-intent prerequisites missing' in text
        or 'payment prerequisites missing' in text
        or 'publishable_key/customer_session_client_secret' in text
        or 'publishable_key/client_secret' in text
    ):
        return 'UPI Checkout 支付前置字段缺失：当前账号或会话未返回 Stripe 支付配置，无法继续提链'
    if 'elements session_id missing' in text:
        return 'UPI Stripe Elements 会话创建失败：上游未返回有效会话编号'
    if 'confirmation_tokens did not return ctoken' in text:
        return 'UPI Stripe 确认令牌创建失败：上游未返回有效确认令牌'
    if 'checkout/confirm missing paymentintent' in text:
        return 'UPI Checkout 确认失败：上游未返回有效 PaymentIntent'
    if 'upi checkout/confirm' in text and 'intent' in text:
        return 'UPI Checkout 确认失败：上游支付对象类型、编号或凭据缺失或不一致'
    if 'payment intent 未生成支付链接' in raw or 'payment intent did not generate' in text:
        return 'UPI 支付意图未生成支付链接：请更换代理或检查账号支付状态'
    if 'payment page 未生成支付链接' in raw or 'payment page did not generate' in text:
        return 'UPI 支付意图未生成支付链接：支付页未返回跳转链接，请更换代理或检查账号支付状态'

    if 'impersonating ' in text and 'not supported' in text:
        return 'UPI 请求协议环境不兼容：当前浏览器指纹不受支持，请更换请求环境'
    if 'unsupported proxy syntax' in text or 'port number was not a decimal' in text:
        return 'UPI 代理格式错误：请使用 host:port:user:password 格式'
    if (
        'proxy authentication required' in text
        or 'failed to receive socks response' in text
        or 'proxy closed connection' in text
        or 'proxy connect' in text
        or 'connect tunnel failed' in text
        or re.search(r'proxy.*connection', text)
    ):
        return 'UPI 代理连接失败：代理认证失败或 SOCKS 通道被上游关闭，请更换代理'
    if 'timed out' in text or 'timeout' in text or 'operation too slow' in text:
        return 'UPI 代理网络超时，请更换代理后重试'
    if 'ssl' in text or 'tls' in text or 'certificate' in text or 'wrong version number' in text:
        return 'UPI 代理安全连接异常，请更换代理后重试'
    if 'network request failed' in text or 'curl:' in text or 'connection reset' in text or 'connection refused' in text:
        return 'UPI 网络请求失败：当前代理或上游连接不可用，请更换代理后重试'

    if 'sentinel bridge module unavailable' in text:
        return 'UPI 风控验证模块不可用，暂时无法继续提链，请联系管理员'
    if 'sentinel bridge returned empty token' in text:
        return 'UPI 风控验证令牌为空，无法继续提链，请更换环境后重试'
    if 'deployment attestation missing' in text or 'browser risk' in text:
        return 'UPI 风控上下文生成失败：当前 Firefox 会话未返回完整风控证明，本次任务已终止'
    if 'checkout create failed' in text:
        if '403' in text or 'blocked' in text or 'risk' in text:
            return 'UPI Checkout 建单被上游风控拒绝，本次任务已终止'
        if '429' in text:
            return 'UPI Checkout 建单请求过于频繁，请稍后更换代理重试'
        return 'UPI Checkout 建单失败：上游未接受当前请求，请更换代理后重试'
    if 'curl_cffi has no usable chrome impersonate profile' in text:
        return 'UPI 请求环境初始化失败：当前运行环境没有可用的浏览器协议指纹'
    if 'proxy is required' in text:
        return 'UPI 提链缺少代理，请配置印度支付代理'
    if 'access token is required' in text:
        return 'UPI 缺少账号登录凭证，请重新提交有效 AT'
    if 'checkout missing oaics session id' in text:
        return 'UPI Checkout 建单失败：上游未返回有效 Checkout 会话编号'
    if 'does not offer upi' in text:
        return 'UPI Checkout 未提供印度 UPI 支付方式，请检查支付代理出口和账号支付资格'
    if 'checkout promotion update failed' in text or 'upi 优惠请求失败' in text:
        return 'UPI 优惠应用失败：上游未接受优惠申请，请检查优惠代理和账号资格'
    if 'chatgpt taxes failed' in text:
        return 'UPI 税费信息请求失败：无法完成印度账单校验'
    if 'stripe confirmation token' in text:
        return 'UPI Stripe 确认令牌请求失败，请更换代理后重试'
    if 'chatgpt checkout confirm failed' in text:
        return 'UPI Checkout 确认请求失败，请更换代理或检查账号状态'
    if 'stripe payment intent confirm failed' in text:
        return 'UPI Stripe 支付确认失败，请更换代理或检查账号支付状态'
    if 'stripe payment intent poll failed' in text:
        return 'UPI 支付状态查询失败，请更换代理后重试'

    if re.search(r'\b(?:http|status(?:\s*code)?)\s*403\b', text) or 'forbidden' in text:
        return 'UPI 上游拒绝了本次请求（403），请检查账号状态和代理网络'
    if re.search(r'\b(?:http|status(?:\s*code)?)\s*429\b', text) or 'too many requests' in text:
        return 'UPI 请求过于频繁（429），请更换代理或稍后重试'
    if re.search(r'\b(?:http|status(?:\s*code)?)\s*5\d\d\b', text) or 'bad gateway' in text:
        return 'UPI 上游服务暂时不可用，请更换代理或稍后重试'

    # cpmt 通道错误(GCash 同款绕过链路):当前会话未提供 UPI 自定义支付方式
    if 'upi cpmt' in text:
        return 'UPI 自定义支付方式通道不可用：当前会话未提供 UPI 自定义支付方式（cpmt），请更换账号或等待上游开放该通道'
    # ChatGPT approve 接口错误(区别于 approve 成功后 Stripe 的 generic_decline)。
    # 只有 risk_blocked 才是风控拦截；普通的 approve 拒绝（例如 result=exception
    # 触发的重建）仍走下面那条，避免把通用失败描述成风控命中。
    if 'checkout approve' in text:
        if 'risk_blocked' in text:
            return 'UPI 订单风控审批未通过：上游返回 blocked，本次任务已终止'
        return 'UPI 订单审批请求被上游拒绝，本次任务已终止'

    # 未知异常统一返回纯中文，避免把上游英文、JSON 或混合错误泄露到前端。
    # 已识别的业务错误在上面优先返回；走到这里说明当前错误没有可靠的细分类。
    return 'UPI 提链失败：上游返回了未识别的错误，请更换代理后重试'
# curl_cffi 0.16.0 当前可实际发请求的 Chrome TLS 档案。
# 印度 UPI HAR 中记录的是 Chrome151，但本机对 chrome151 只会在
# Session.request() 阶段抛出"Impersonating chrome151 is not supported"；
# GCash 同样使用 chrome136，因此 UPI 不再把 HAR 的浏览器版本硬套到
# curl_cffi，避免请求尚未发出就失败。UA/sec-ch-ua 与 TLS 档案保持同版本。
IN_CHROME_PROFILES = (
    {'impersonate': 'chrome136', 'major': 136, 'build': 7103, 'patch_range': (48, 175), 'sec_ch_ua': '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'},
    {'impersonate': 'chrome131', 'major': 131, 'build': 6778, 'patch_range': (69, 205), 'sec_ch_ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'},
    {'impersonate': 'chrome124', 'major': 124, 'build': 6367, 'patch_range': (60, 207), 'sec_ch_ua': '"Chromium";v="124", "Google Chrome";v="124", "Not.A-Brand";v="99"'},
    {'impersonate': 'chrome116', 'major': 116, 'build': 5845, 'patch_range': (96, 220), 'sec_ch_ua': '"Chromium";v="116", "Google Chrome";v="116", "Not.A/Brand";v="24"'},
)
# Edge / Firefox TLS 目标（与 src/fingerprint.js 的内核池一致，用于跨内核族轮换）。
# 只在部署环境 curl_cffi 支持时才被选中；不支持时回退 Chrome 家族。
IN_EDGE_PROFILES = (
    {'impersonate': 'edge101', 'major': 101, 'edge': '101.0.1210.47', 'sec_ch_ua': '"Microsoft Edge";v="101", "Not.A/Brand";v="99", "Chromium";v="101"'},
    {'impersonate': 'edge99', 'major': 99, 'edge': '99.0.1150.46', 'sec_ch_ua': '"Microsoft Edge";v="99", "Not.A/Brand";v="8", "Chromium";v="99"'},
)
IN_FIREFOX_PROFILES = (
    {'impersonate': 'firefox133', 'major': 133},
    {'impersonate': 'firefox128', 'major': 128},
)
# upi-git5 专用 macOS TLS 档案，与源站 UPI_GIT5_MACOS_PROFILES 同值。
# 该 rail 固定以 macOS 身份出现（chrome 带 sec-ch-ua-platform: "macOS"，
# firefox 不带任何 sec-ch-ua），并且完全不启动浏览器。
UPI_GIT5_RAIL = 'upi-git5'
UPI_GIT5_MACOS_PROFILES = ('chrome146-macos', 'chrome150-macos', 'firefox147-macos', 'chrome136-macos')
UPI_GIT5_ACCEPT_LANGUAGE = 'en-IN,en;q=0.9,hi;q=0.8'
UPI_GIT5_BROWSER_LOCALE = 'en-IN'
UPI_GIT5_CHROME_PROFILES = (
    {'impersonate': 'chrome146', 'major': 146, 'build': 7600, 'patch_range': (20, 190), 'sec_ch_ua': '"Chromium";v="146", "Google Chrome";v="146", "Not.A/Brand";v="99"'},
    {'impersonate': 'chrome150', 'major': 150, 'build': 7800, 'patch_range': (20, 190), 'sec_ch_ua': '"Chromium";v="150", "Google Chrome";v="150", "Not.A/Brand";v="99"'},
)
UPI_GIT5_FIREFOX_PROFILES = (
    {'impersonate': 'firefox147', 'major': 147},
)
UPI_GIT5_MACOS_SUFFIX = '-macos'
# 协议模式的 attestation 来源标记。刻意不用 'browser'，否则会污染诊断日志、
# 并让"到底走没走浏览器"无法从结果里分辨。
UPI_PROTOCOL_ATTESTATION_SOURCE = 'protocol_macos'
UPI_ATTESTATION_SOURCES = ('browser', 'browser_hydration', UPI_PROTOCOL_ATTESTATION_SOURCE)
IN_ACCEPT_LANGUAGE = 'hi-IN,hi;q=0.9'
IN_BROWSER_LOCALE = 'hi-IN'
VN_ACCEPT_LANGUAGE = 'vi-VN,vi;q=0.9,en;q=0.8'
VN_BROWSER_LOCALE = 'vi-VN'
# 提链提速:压缩步骤间模拟延迟(原 short/med/long = 0.8-1.8/1.5-3.2/2.5-5.0s,
# 每次尝试 10 处 pause 合计约 21s,是纯等待;降到约 5s 仍保留轻微节奏感)
HUMAN_DELAY_SHORT = (0.3, 0.7)
HUMAN_DELAY_MED = (0.6, 1.2)
HUMAN_DELAY_LONG = (1.2, 2.2)
REGION_HANDOFF_DELAY = (1.5, 2.8)
CHATGPT_CLIENT_VERSION = 'prod-05e15ce020ae32b10bcc0042da44e5a3853ae67c'
CHATGPT_CLIENT_BUILD = '9808190'
SENTINEL_VERSION = '20260810913b'
STRIPE_VERSION = '2025-03-31.basil'
STRIPE_RUNTIME_VERSION = '6f8494a281'
UPI_BILLING_NAME = 'Raj Kumar'
UPI_BILLING_LINE1 = '123 MG Road'
UPI_BILLING_CITY = 'Mumbai'
UPI_BILLING_STATE = 'MH'
UPI_BILLING_POSTAL = '400001'
UPI_PROMO_COUPON = 'plus-1-month-free'
INDIA_STATE_NAMES = {
    'AP': 'Andhra Pradesh',
    'AR': 'Arunachal Pradesh',
    'AS': 'Assam',
    'BR': 'Bihar',
    'CG': 'Chhattisgarh',
    'CH': 'Chandigarh',
    'DL': 'Delhi',
    'GA': 'Goa',
    'GJ': 'Gujarat',
    'HR': 'Haryana',
    'HP': 'Himachal Pradesh',
    'JK': 'Jammu and Kashmir',
    'JH': 'Jharkhand',
    'KA': 'Karnataka',
    'KL': 'Kerala',
    'MP': 'Madhya Pradesh',
    'MH': 'Maharashtra',
    'MN': 'Manipur',
    'ML': 'Meghalaya',
    'MZ': 'Mizoram',
    'NL': 'Nagaland',
    'OD': 'Odisha',
    'PB': 'Punjab',
    'RJ': 'Rajasthan',
    'SK': 'Sikkim',
    'TN': 'Tamil Nadu',
    'TS': 'Telangana',
    'TR': 'Tripura',
    'UK': 'Uttarakhand',
    'UP': 'Uttar Pradesh',
    'WB': 'West Bengal',
}
DEFAULT_LOCAL_PROXY = ""
DEFAULT_UPI_BILLING = {
    'name': 'Raj Kumar',
    'email': '',
    'address': {
        'line1': '123 MG Road',
        'city': 'Mumbai',
        'state': 'MH',
        'postal_code': '400001',
        'country': 'IN',
    },
}
# UPI 账单姓名池（印度常见名，全随机；仅在没有显式账单参数时使用）。
UPI_BILLING_NAMES = (
    'Arjun Sharma', 'Rahul Verma', 'Priya Patel', 'Ananya Singh', 'Aarav Mehta',
    'Diya Iyer', 'Rohan Krishnan', 'Kavya Reddy', 'Vikram Nair', 'Ishita Kapoor',
    'Aditya Deshmukh', 'Sneha Joshi', 'Karan Malhotra', 'Neha Gupta', 'Siddharth Rao',
    'Pooja Kulkarni', 'Varun Bhat', 'Anjali Menon', 'Nikhil Saxena', 'Ritu Agarwal',
    'Aman Chopra', 'Shreya Ghosh', 'Dev Patel', 'Meera Pillai', 'Harsh Vardhan',
    'Tanvi Shah', 'Ravi Kumar', 'Lakshmi Narayanan', 'Arnav Kohli', 'Anushka Bansal',
    'Kabir Sethi', 'Zoya Khan', 'Manav Trivedi', 'Simran Kaur', 'Yash Khurana',
)
# 内置印度地址候选：78 条，覆盖 49 个城市、24 个邦/联邦属地。
# 这些静态记录提供本地格式/区域校验依据，不代表邮政地址认证。
UPI_BILLING_ADDRESS_POOL = (
    ('Apollo Bunder, Colaba', 'Mumbai', 'MH', '400001'),
    ('Nariman Point', 'Mumbai', 'MH', '400021'),
    ('Diplomatic Enclave, Chanakyapuri', 'New Delhi', 'DL', '110023'),
    ('Sardar Patel Marg, Diplomatic Enclave', 'New Delhi', 'DL', '110021'),
    ('Janpath', 'New Delhi', 'DL', '110001'),
    ('Barakhamba Avenue', 'New Delhi', 'DL', '110001'),
    ('1 Residency Road', 'Bengaluru', 'KA', '560025'),
    ('37-39 MG Road', 'Bengaluru', 'KA', '560001'),
    ('23 Old Airport Road', 'Bengaluru', 'KA', '560008'),
    ('26/1 Dr Rajkumar Road', 'Bengaluru', 'KA', '560055'),
    ('37 Mahatma Gandhi Road, Nungambakkam', 'Chennai', 'TN', '600034'),
    ('365 Anna Salai, Teynampet', 'Chennai', 'TN', '600018'),
    ('154 Velachery Main Road', 'Chennai', 'TN', '600042'),
    ('17 Park Street', 'Kolkata', 'WB', '700016'),
    ('34B Belvedere Road, Alipore', 'Kolkata', 'WB', '700027'),
    ('Engine Bowli, Falaknuma', 'Hyderabad', 'TS', '500053'),
    ('Road No. 1, Banjara Hills', 'Hyderabad', 'TS', '500034'),
    ('1-10-147, Begumpet', 'Hyderabad', 'TS', '500016'),
    ('Bhawani Singh Road', 'Jaipur', 'RJ', '302005'),
    ('2, Riico Kukas', 'Jaipur', 'RJ', '302028'),
    ('Senapati Bapat Road', 'Pune', 'MH', '411053'),
    ('7/7 Mangaldas Road', 'Pune', 'MH', '411001'),
    ('Ramdevnagar Road', 'Ahmedabad', 'GJ', '380054'),
    ('SG Highway', 'Ahmedabad', 'GJ', '380054'),
    ('178, Industrial Area Phase 1', 'Chandigarh', 'CH', '160002'),
    ('8 Mahatma Gandhi Marg, Hazratganj', 'Lucknow', 'UP', '226001'),
    ('DB City Mall, Arera Hills', 'Bhopal', 'MP', '462011'),
    ('South Gandhi Maidan', 'Patna', 'BR', '800001'),
    ('1, Airport Road, Hinoo', 'Ranchi', 'JH', '834002'),
    ('Toshali Plaza, Sahid Nagar', 'Bhubaneswar', 'OD', '751007'),
    ('National Highway 37, Gotanagar', 'Guwahati', 'AS', '781033'),
    ('Marine Drive', 'Kochi', 'KL', '682031'),
    ('Beach Road', 'Visakhapatnam', 'AP', '530023'),
    ('C V Raman Pillai Road, Kowdiar', 'Thiruvananthapuram', 'KL', '695003'),
    ('66, Avanashi Road', 'Coimbatore', 'TN', '641037'),
    ('12, Linking Road, Bandra West', 'Mumbai', 'MH', '400050'),
    ('18, Palm Beach Road, Vashi', 'Navi Mumbai', 'MH', '400703'),
    ('Pokhran Road No. 2, Majiwada', 'Thane', 'MH', '400601'),
    ('Lane 5, Koregaon Park', 'Pune', 'MH', '411001'),
    ('College Road, Canada Corner', 'Nashik', 'MH', '422005'),
    ('Civil Lines, Wardha Road', 'Nagpur', 'MH', '440001'),
    ('C G Road, Navrangpura', 'Ahmedabad', 'GJ', '380009'),
    ('Race Course Road, Alkapuri', 'Vadodara', 'GJ', '390007'),
    ('Ghod Dod Road, Athwa', 'Surat', 'GJ', '395007'),
    ('Sector 29, Near Leisure Valley', 'Gurugram', 'HR', '122001'),
    ('Sector 17, Madhya Marg', 'Chandigarh', 'CH', '160017'),
    ('Ranjit Avenue, Block B', 'Amritsar', 'PB', '143001'),
    ('Ferozepur Road, Model Town', 'Ludhiana', 'PB', '141001'),
    ('Rajpur Road, Hathibarkala', 'Dehradun', 'UK', '248001'),
    ('The Mall Road, Chotta Shimla', 'Shimla', 'HP', '171002'),
    ('Gandhi Nagar, Green Belt Road', 'Jammu', 'JK', '180004'),
    ('Residency Road, Lal Chowk', 'Srinagar', 'JK', '190001'),
    ('Sector 18, Atta Market Road', 'Noida', 'UP', '201301'),
    ('Gomti Nagar, Vibhuti Khand', 'Lucknow', 'UP', '226010'),
    ('Mall Road, Civil Lines', 'Kanpur', 'UP', '208001'),
    ('M G Marg, Civil Lines', 'Prayagraj', 'UP', '211001'),
    ('Vijay Nagar, A B Road', 'Indore', 'MP', '452010'),
    ('Napier Town, South Civil Lines', 'Jabalpur', 'MP', '482001'),
    ('G E Road, Telibandha', 'Raipur', 'CG', '492001'),
    ('Circular Road, Lalpur', 'Ranchi', 'JH', '834001'),
    ('Main Road, Bistupur', 'Jamshedpur', 'JH', '831001'),
    ('Jayadev Vihar, Nandankanan Road', 'Bhubaneswar', 'OD', '751013'),
    ('Buxi Bazar Road', 'Cuttack', 'OD', '753001'),
    ('G S Road, Christian Basti', 'Guwahati', 'AS', '781005'),
    ('Sevoke Road, Ward 10', 'Siliguri', 'WB', '734001'),
    ('Sector V, Salt Lake', 'Kolkata', 'WB', '700091'),
    ('Indiranagar 100 Feet Road', 'Bengaluru', 'KA', '560038'),
    ('Sayyaji Rao Road, Devaraja Mohalla', 'Mysuru', 'KA', '570001'),
    ('K S Rao Road, Hampankatta', 'Mangaluru', 'KA', '575001'),
    ('Sardar Patel Road, Adyar', 'Chennai', 'TN', '600020'),
    ('Race Course Road, Gopalapuram', 'Coimbatore', 'TN', '641018'),
    ('Alagar Kovil Road, Tallakulam', 'Madurai', 'TN', '625002'),
    ('Hitech City Main Road, Madhapur', 'Hyderabad', 'TS', '500081'),
    ('M G Road, Labbipet', 'Vijayawada', 'AP', '520010'),
    ('Air Bypass Road, New Balaji Colony', 'Tirupati', 'AP', '517501'),
    ('M G Road, Ernakulam', 'Kochi', 'KL', '682016'),
    ('Mavoor Road, Arayidathupalam', 'Kozhikode', 'KL', '673004'),
    ('M G Road, Panaji Market', 'Panaji', 'GA', '403001'),
)

# 城市查询仅供显式启用同城选择且未填写地址时使用，按代理缓存。
_EXIT_CITY_BY_PROXY: dict[str, str] = {}
_UPI_BILLING_PICK_LOCK = threading.Lock()
_UPI_LAST_BILLING_ROW: tuple[str, str, str, str] | None = None

_UPI_CITY_ALIASES = {
    'bangalore': 'Bengaluru', 'bombay': 'Mumbai', 'gurgaon': 'Gurugram',
    'calcutta': 'Kolkata', 'cochin': 'Kochi', 'madras': 'Chennai',
    'trivandrum': 'Thiruvananthapuram', 'mysore': 'Mysuru',
    'mangalore': 'Mangaluru', 'calicut': 'Kozhikode', 'panjim': 'Panaji',
    'allahabad': 'Prayagraj', 'delhi': 'New Delhi',
}


def _upi_city_name(value: str) -> str:
    city = ' '.join(str(value or '').split())
    return _UPI_CITY_ALIASES.get(city.casefold(), city)


def _upi_state_code(value: str) -> str:
    state = ' '.join(str(value or '').split()).casefold()
    for code, name in INDIA_STATE_NAMES.items():
        if state in {code.casefold(), name.casefold()}:
            return code
    return ''


def _upi_billing_address_rows() -> list[tuple[str, str, str, str]]:
    """Merge static candidates once per selection, preserving equal row weights."""
    candidates = list(UPI_BILLING_ADDRESS_POOL)
    try:
        from billing_address_resolver import _BUILTIN_PUBLIC_ADDRESSES
    except ImportError:
        resolver_rows = []
    else:
        resolver_rows = _BUILTIN_PUBLIC_ADDRESSES.get('IN') or []
    for row in resolver_rows:
        if isinstance(row, dict) and str(row.get('country') or 'IN').strip().upper() == 'IN':
            candidates.append(tuple(row.get(key) for key in ('line1', 'city', 'state', 'postal_code')))
    result = []
    seen = set()
    for row in candidates:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            continue
        line1, city, state, postal = (str(value or '').strip() for value in row)
        line1 = ' '.join(line1.split())
        city, state = _upi_city_name(city), _upi_state_code(state)
        if not line1 or not city or not state or not re.fullmatch(r'[1-9][0-9]{5}', postal):
            continue
        signature = (line1.casefold(), city.casefold(), state, postal)
        if signature not in seen:
            seen.add(signature)
            result.append((line1, city, state, postal))
    return result


def _probe_upi_exit_city(proxy: str) -> str:
    if not proxy:
        return ''
    try:
        import importlib.util
        import os as _os
        _gopay_dir = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..', '..', 'gopay_engine'))
        _spec = importlib.util.spec_from_file_location('_upi_id_billing_probe', _os.path.join(_gopay_dir, 'id_billing.py'))
        if not _spec or not _spec.loader:
            return ''
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _fn = getattr(_mod, 'probe_exit_city', None)
        if not callable(_fn):
            return ''
        city = str(_fn(proxy=proxy, timeout=5.0) or '').strip()
        return city.split(',')[0].strip() if city else ''
    except Exception:
        return ''


def _resolver_indian_address_row() -> tuple | None:
    """Compatibility helper; UPI selection uses the deduplicated full pool."""
    try:
        from billing_address_resolver import resolve_public_address  # 同目录模块
        resolved = resolve_public_address('IN', '', '', '')
    except Exception:
        return None
    if not isinstance(resolved, dict):
        return None
    line1 = str(resolved.get('line1') or '').strip()
    city = str(resolved.get('city') or '').strip()
    state = str(resolved.get('state') or '').strip()
    postal = str(resolved.get('postal_code') or '').strip()
    if line1 and city and re.fullmatch(r'[A-Za-z]{2}', state) and re.fullmatch(r'\d{6}', postal):
        return (line1, city, state.upper(), postal)
    return None


def _random_upi_billing_address(preferred_city: str = '') -> dict[str, str]:
    """Pick one whole address; avoid consecutive repeats within this process."""
    global _UPI_LAST_BILLING_ROW
    rng = random.SystemRandom()
    pool = _upi_billing_address_rows()
    if not pool:
        raise ValueError('UPI 账单地址池为空或没有有效记录')
    city_hint = _upi_city_name(preferred_city).casefold()
    if city_hint:
        same = [row for row in pool if row[1].casefold() == city_hint]
        if same:
            pool = same
    with _UPI_BILLING_PICK_LOCK:
        choices = [row for row in pool if row != _UPI_LAST_BILLING_ROW] or pool
        selected = rng.choice(choices)
        _UPI_LAST_BILLING_ROW = selected
    line1, city, state, postal = selected
    return {
        'name': rng.choice(UPI_BILLING_NAMES),
        'email': '',
        'address': {
            'line1': line1,
            'city': city,
            'state': state,
            'postal_code': postal,
            'country': 'IN',
        },
    }


def _known_indian_cities() -> set[str]:
    return {row[1].casefold() for row in _upi_billing_address_rows()}


def _is_complete_indian_address(line1: str, city: str, state: str, postal: str, country: str = 'IN') -> bool:
    """Check known city/state/PIN-prefix relationships, not postal deliverability."""
    if str(country or '').strip().casefold() not in {'in', 'india'}:
        return False
    city_text = _upi_city_name(city).casefold()
    state_code = _upi_state_code(state)
    postal_text = str(postal or '').strip()
    if not str(line1 or '').strip() or not city_text or not state_code:
        return False
    if not re.fullmatch(r'[1-9][0-9]{5}', postal_text):
        return False
    return any(
        row_city.casefold() == city_text and row_state == state_code and row_postal[:3] == postal_text[:3]
        for _, row_city, row_state, row_postal in _upi_billing_address_rows()
    )


def _upi_billing_sync_city() -> bool:
    """账单是否与代理出口同城（默认关闭：地址走全印度随机）。"""
    return str(os.environ.get('UPI_BILLING_SYNC_CITY') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
UPI_SCHEME_RE = re.compile('upi://pay[^\\s\\"\'<>]*', re.I)
STRIPE_HOSTED_RE = re.compile('https://hooks\\.stripe\\.com/redirect/[^\\s\\"\'<>]+', re.I)
STRIPE_UPI_INSTRUCTIONS_RE = re.compile('https://payments\\.stripe\\.com/upi/instructions/[^\\s\\"\'<>]+', re.I)
# txt 文档 Step 7 要求保留 pm-redirects 提取兜底(与 GCash/GoPay 同形态的 Stripe 授权跳转)
PM_REDIRECTS_RE = re.compile(r"https://pm-redirects\.stripe\.com/[^\s\"'<>]+", re.I)
SESSION_COOKIE_NAMES = ('__Secure-next-auth.session-token', 'next-auth.session-token')
MAX_REDIRECT_POLL_ATTEMPTS = 15
APPROVED_REDIRECT_POLL_ATTEMPTS = 20
# UPI 二维码轮询上限：二维码有效期约 5 分钟且随轮询刷新，2s 间隔 × 120 ≈ 4 分钟，
# 期间持续推送最新二维码，status=succeeded 即到账。
UPI_PAYMENT_POLL_ATTEMPTS = 120

def _project_root() -> str:
    return str(os.environ.get('VERIFY_APP_ROOT') or os.environ.get('APP_ROOT') or '').strip() or os.path.dirname(__file__)

def _mask_proxy_summary(proxy: str) -> str:
    text = str(proxy or '').strip()
    if not text:
        return ''
    if '@' in text:
        scheme, rest = text.split('://', 1) if '://' in text else ('', text)
        creds, host = rest.rsplit('@', 1)
        user = creds.split(':', 1)[0]
        masked_user = user[:4] + '***' if len(user) > 4 else '***'
        return f'{scheme}://{masked_user}:***@{host}' if scheme else f'{masked_user}:***@{host}'
    return text[:24] + '...' if len(text) > 24 else text

def _proxy_with_fresh_sid(proxy: str) -> str:
    proxy = str(proxy or '').strip()
    if not proxy:
        return proxy
    return re.sub('sid-[^-:@]*-t-', f'sid-{uuid.uuid4().hex[:8]}-t-', proxy)


def _normalize_upi_proxy_url(raw: str) -> str:
    """Normalize proxy URLs before handing them to curl_cffi.

    Some 1024Proxy exports are copied in a compound form such as:
    ``http://user@host:port@socks5h://password``. That is not a proxy URL;
    it is the host endpoint and the credential password accidentally joined
    with a second protocol marker. Convert only this UPI-specific legacy
    form to the standard ``scheme://user:password@host:port`` form. Normal
    proxy URLs are returned unchanged so other channel behavior is untouched.
    """
    text = str(raw or '').strip()
    if not text:
        return ''

    # With an explicit outer scheme, keep that scheme as the transport used
    # by the endpoint; the inner scheme marker is stripped from the password.
    compound = re.fullmatch(
        r'(?P<scheme>https?|socks5h?)://'
        r'(?P<user>[^@:/?#]+)@(?P<host>[^/:@?#]+):(?P<port>\d+)@'
        r'(?P<inner>socks5h?|https?)://(?P<password>.+)',
        text,
        flags=re.I,
    )
    if compound:
        scheme = compound.group('scheme').lower()
        user = quote(compound.group('user'), safe='')
        password = quote(compound.group('password'), safe='')
        return f"{scheme}://{user}:{password}@{compound.group('host')}:{compound.group('port')}"

    # Also accept the same malformed shape without the leading scheme.
    compound_no_outer = re.fullmatch(
        r'(?P<user>[^@:/?#]+)@(?P<host>[^/:@?#]+):(?P<port>\d+)@'
        r'(?P<inner>socks5h?|https?)://(?P<password>.+)',
        text,
        flags=re.I,
    )
    if compound_no_outer:
        user = quote(compound_no_outer.group('user'), safe='')
        password = quote(compound_no_outer.group('password'), safe='')
        return f"http://{user}:{password}@{compound_no_outer.group('host')}:{compound_no_outer.group('port')}"

    # 直接调用 UPI CLI 时也接受代理池原始格式：host:port:user:pass。
    # 主进程通常会先通过 src/proxy.js 转成 URL，但这里再做一次归一化，
    # 防止绕过 Node 入口时把 host:port:user:pass 原样交给 curl_cffi。
    if '://' not in text:
        parts = text.split(':', 3)
        if len(parts) == 4 and parts[1].isdigit():
            host, port, user, password = parts
            if host and 0 < int(port) <= 65535 and user:
                return f"http://{quote(user, safe='-._~')}:{quote(password, safe='-._~')}@{host}:{port}"

    return text

def _load_env_file() -> None:
    path = os.path.join(_project_root(), 'env_upi_link.txt')
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                if not key.startswith('UPI_LINK_'):
                    continue
                val = value.strip().strip('"').strip("'")
                if val and (not str(os.environ.get(key) or '').strip()):
                    os.environ[key] = val
    except Exception:
        pass

def resolve_upi_proxy() -> str:
    _load_env_file()
    for key in ('UPI_LINK_PROXY', 'OPENAI_PAY_DEFAULT_PROXY'):
        val = str(os.environ.get(key) or '').strip()
        if val:
            return val
    return DEFAULT_LOCAL_PROXY

def _load_chatgpt_rt_env_file() -> None:
    path = os.path.join(_project_root(), 'env_chatgpt_rt.txt')
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                if not key.startswith('CHATGPT_RT_'):
                    continue
                val = value.strip().strip('"').strip("'")
                if val and (not str(os.environ.get(key) or '').strip()):
                    os.environ[key] = val
    except Exception:
        pass

def _parse_rt_proxy_line(raw_line: str) -> str:
    text = str(raw_line or '').strip()
    if not text:
        return ''
    if '://' in text:
        return text
    parts = text.split(':')
    if len(parts) >= 4:
        host, port, user = (parts[0], parts[1], parts[2])
        password = ':'.join(parts[3:])
        user_q = quote(user, safe='')
        pass_q = quote(password, safe='')
        return f'http://{user_q}:{pass_q}@{host}:{port}'
    if len(parts) == 2:
        return f'http://{parts[0]}:{parts[1]}'
    return ''

def _rt_proxy_from_env() -> str:
    _load_chatgpt_rt_env_file()
    direct = str(os.environ.get('CHATGPT_RT_PROXY') or '').strip()
    if direct:
        return direct
    line = str(os.environ.get('CHATGPT_RT_PROXY_LINE') or '').strip()
    if line:
        return _parse_rt_proxy_line(line)
    return ''

def resolve_upi_promotion_proxy(base_proxy: str='') -> str:
    _load_env_file()
    dedicated = str(os.environ.get('PAY153_UPI_PROMOTION_PROXY') or os.environ.get('UPI_LINK_PROMOTION_PROXY') or '').strip()
    if dedicated:
        return proxy_for_region(dedicated, PROMOTION_REGION) or dedicated
    base = str(base_proxy or resolve_upi_proxy()).strip()
    return proxy_for_region(base, PROMOTION_REGION) if base else ''

def _uses_lowercase_proxy_region(proxy: str) -> bool:
    text = str(proxy or '').strip().lower()
    return 'iproyal' in text or ('://' not in text and text.count(':') >= 4)

def proxy_for_region(proxy: str, region: str) -> str:
    proxy = str(proxy or '').strip()
    region = str(region or '').strip()
    if not proxy or not region:
        return proxy
    region_val = region.lower() if _uses_lowercase_proxy_region(proxy) else region.upper()
    rewritten = re.sub('region-[A-Za-z]{2}', f'region-{region_val}', proxy)
    if region.upper() != 'JP':
        rewritten = re.sub('-st-[^-:@]+-sid-', '-sid-', rewritten)
    return rewritten

def normalize_access_token(raw: str) -> str:
    text = str(raw or '').strip()
    if text.lower().startswith('bearer '):
        text = text[7:].strip()
    if not text:
        return ''
    if text.startswith('{') or text.startswith('['):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ''
        return _find_token(data) or ''
    return text

def _parse_session_object(raw: str) -> dict[str, Any]:
    text = str(raw or '').strip()
    if not text.startswith('{'):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}

def _extract_session_token_from_raw(raw: str) -> str:
    return str(_parse_session_object(raw).get('sessionToken') or '').strip()

def _refresh_access_token_from_session(session_token: str, proxy: str, device_id: str,
                                       fingerprint: dict[str, Any] | None = None) -> str:
    token = str(session_token or '').strip()
    if not token:
        return ''
    proxy_url = str(proxy or '').strip()
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    session = _new_upi_http_session(str(fp.get('impersonate') or 'chrome131'))
    for name in SESSION_COOKIE_NAMES:
        try:
            session.cookies.set(name, token, domain='chatgpt.com', path='/')
        except Exception:
            pass
    try:
        refresh_headers = {
            'Accept': 'application/json', 'Referer': 'https://chatgpt.com/',
            'Origin': 'https://chatgpt.com', 'oai-device-id': device_id,
        }
        if fp.get('ua'):
            refresh_headers['User-Agent'] = str(fp.get('ua'))
        if fp.get('accept_language'):
            refresh_headers['Accept-Language'] = str(fp.get('accept_language'))
        r = session.get('https://chatgpt.com/api/auth/session', headers=refresh_headers, proxies={'http': proxy_url, 'https': proxy_url} if proxy_url else None, timeout=30)
        if r.status_code != 200:
            return ''
        data = r.json() if r.text else {}
        if not isinstance(data, dict):
            return ''
        return str(data.get('accessToken') or '').strip()
    except Exception as exc:
        if localize_upi_error(exc) == UPI_TLS_CONFIGURATION_ERROR:
            raise RuntimeError(UPI_TLS_CONFIGURATION_ERROR) from exc
        return ''
    finally:
        session.close()

def resolve_upi_access_token(
    raw: str,
    proxy: str = '',
    fingerprint: dict[str, Any] | None = None,
    client: UpiHttpClient | None = None,
) -> str:
    token = normalize_access_token(raw)
    if not token:
        return ''
    proxy_url = str(proxy or resolve_upi_proxy() or '').strip()
    if not proxy_url:
        return token
    active_client = client or UpiHttpClient(proxy_url, region='IN', fingerprint=fingerprint)
    owns_client = client is None
    try:
        st, _, _ = active_client.request(
            'GET',
            'https://chatgpt.com/backend-api/me',
            headers=_chatgpt_headers(active_client, token),
            proxy=proxy_url,
        )
        if st == 200:
            return token
        session_token = _extract_session_token_from_raw(raw)
        if not session_token:
            return token
        fresh = _refresh_access_token_from_session(
            session_token, proxy_url, active_client.device_id, fingerprint
        )
        if not fresh:
            return token
        st2, _, _ = active_client.request(
            'GET',
            'https://chatgpt.com/backend-api/me',
            headers=_chatgpt_headers(active_client, fresh),
            proxy=proxy_url,
        )
        return fresh if st2 == 200 else token
    finally:
        if owns_client:
            active_client.close()

def _find_token(value: Any) -> str | None:
    if isinstance(value, str):
        token = normalize_access_token(value)
        return token or None
    if isinstance(value, list):
        for item in value:
            found = _find_token(item)
            if found:
                return found
        return None
    if isinstance(value, dict):
        for key in ('accessToken', 'access_token', 'token'):
            if key in value:
                found = _find_token(value.get(key))
                if found:
                    return found
        for item in value.values():
            found = _find_token(item)
            if found:
                return found
    return None

def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split('.')[1]
        pad = '=' * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + pad))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _jwt_account_id(token: str) -> str:
    auth = _jwt_payload(token).get('https://api.openai.com/auth') or {}
    if isinstance(auth, dict):
        return str(auth.get('chatgpt_account_id') or '').strip()
    return ''

def _jwt_email(token: str) -> str:
    data = _jwt_payload(token)
    if not data:
        return 'buyer@example.com'
    profile = data.get('https://api.openai.com/profile') or {}
    if isinstance(profile, dict):
        email = str(profile.get('email') or profile.get('email_address') or '').strip()
        if '@' in email:
            return email
    email = str(data.get('email') or data.get('preferred_username') or '').strip()
    return email if '@' in email else 'buyer@example.com'

def _fetch_promo_coupon_state(
    token: str,
    proxy: str,
    promotion_proxy: str = '',
    fingerprint: dict[str, Any] | None = None,
    client: UpiHttpClient | None = None,
    promo_campaign: str = UPI_PROMO_COUPON,
) -> str:
    active_client = client or _spawn_region_client(
        promotion_proxy or proxy, PROMOTION_REGION, fingerprint=fingerprint
    )
    owns_client = client is None
    try:
        # UPI promotion preflight uses the same IN egress and browser locale
        # as Checkout. The older VN override made the request identity disagree
        # with the payment country after PROMOTION_REGION was moved to IN.
        headers = _chatgpt_headers(active_client, token, promotion=False)
        account_id = _jwt_account_id(token)
        if account_id:
            headers['ChatGPT-Account-Id'] = account_id
        promo_proxy = promotion_proxy or active_client.base_proxy or resolve_upi_promotion_proxy(proxy)
        campaign = str(promo_campaign or UPI_PROMO_COUPON).strip()
        url = (
            'https://chatgpt.com/backend-api/promo_campaign/check_coupon?'
            + urlencode({'coupon': campaign, 'is_coupon_from_query_param': 'true'})
        )
        status, text, _ = active_client.request('GET', url, headers=headers, proxy=promo_proxy)
        if status >= 400:
            return 'unknown'
        try:
            data = json.loads(text or '{}')
        except json.JSONDecodeError:
            return 'unknown'
        if not isinstance(data, dict):
            return 'unknown'
        state = str(data.get('state') or '').strip().lower()
        if state == 'eligible':
            return 'eligible'
        redemption = data.get('redemption') if isinstance(data.get('redemption'), dict) else {}
        if redemption.get('redeemed_by_user') or redemption.get('redeemed'):
            return 'already_redeemed'
        if state in ('not_eligible', 'ineligible'):
            return 'not_eligible'
        return state or 'unknown'
    finally:
        if owns_client:
            active_client.close()

def _assert_upi_promo_eligible(token: str, proxy: str, promotion_proxy: str = '', fingerprint: dict[str, Any] | None = None) -> None:
    state = _fetch_promo_coupon_state(token, proxy, promotion_proxy, fingerprint=fingerprint)
    if state in ('eligible', 'unknown'):
        return
    if state == 'already_redeemed':
        raise RuntimeError('upi promo already redeemed')
    raise RuntimeError(f'upi promo not eligible: state={state}')

def _curl_impersonate_supported(name: str) -> bool:
    """是否当前 curl_cffi 支持该 impersonate 目标。"""
    try:
        session = curl_requests.Session(impersonate=name)
        session.close()
        return True
    except Exception:
        return False


def _upi_rail_str(rail: Any) -> str:
    return str(rail or 'upi').strip().lower() or 'upi'


def _upi_is_protocol_rail(rail: Any) -> bool:
    """源站里只有 upi-git5 是"纯协议 · 无浏览器内核"，其余 rail 仍驱动真 Firefox。"""
    return _upi_rail_str(rail) == UPI_GIT5_RAIL


def _upi_protocol_client(client: Any) -> bool:
    return _upi_is_protocol_rail(getattr(client, 'upi_rail', 'upi'))


def _pick_india_profile(kernel: str = 'chrome', requested_impersonate: str = '') -> dict[str, Any]:
    """按内核族/请求的 impersonate 选取一套可用的浏览器 TLS 指纹。

    内核池 chrome/edge/firefox 与 src/fingerprint.js 一致；显式指定了
    impersonate 时必须采用该目标；环境不支持时立即失败，避免 UPI 在
    Firefox UA 下静默改用 Chrome TLS 指纹。

    `chrome146-macos` 这类带 -macos 后缀的目标：后缀只表示平台（由调用方的
    指纹档案决定 UA/platform 字段），这里剥掉后再查 TLS 表。
    """
    kernel = str(kernel or 'chrome').strip().lower()
    requested = str(requested_impersonate or '').strip().lower()
    if requested.endswith(UPI_GIT5_MACOS_SUFFIX):
        requested = requested[: -len(UPI_GIT5_MACOS_SUFFIX)]
    tables = {
        'chrome': IN_CHROME_PROFILES + UPI_GIT5_CHROME_PROFILES,
        'edge': IN_EDGE_PROFILES,
        'firefox': IN_FIREFOX_PROFILES + UPI_GIT5_FIREFOX_PROFILES,
    }
    profiles = tables.get(kernel) or (IN_CHROME_PROFILES + UPI_GIT5_CHROME_PROFILES)

    if requested:
        match = next((item for item in profiles if str(item.get('impersonate')) == requested), None)
        if match is None:
            raise RuntimeError(
                f'curl_cffi impersonate profile {requested_impersonate} does not belong to kernel={kernel}'
            )
        if not _curl_impersonate_supported(requested_impersonate):
            raise RuntimeError(f'curl_cffi does not support required impersonate profile: {requested_impersonate}')
        return match

    # 在该内核族内顺序选第一个可用目标，不跨内核回退。
    last_err: Exception | None = None
    for profile in profiles:
        name = str(profile['impersonate'])
        if _curl_impersonate_supported(name):
            return profile
        last_err = RuntimeError(name)
    raise RuntimeError(f'curl_cffi has no usable impersonate profile for kernel={kernel}: {last_err}')

def _upi_env_client_observation() -> str:
    """Deprecated: UPI observations must come from this attempt's browser."""
    return ''


class UpiHttpClient:

    def __init__(self, proxy: str, *, region: str='IN', fingerprint: dict[str, Any] | None = None, upi_rail: str = 'upi'):
        self.base_proxy = str(proxy or '').strip()
        self.proxy = self.base_proxy
        self.region = str(region or 'IN').upper()
        # 该次尝试使用的 rail。upi-git5 走纯协议（不启动浏览器），其余走 Firefox。
        self.upi_rail = _upi_rail_str(upi_rail)
        supplied = fingerprint if isinstance(fingerprint, dict) else {}
        requested_impersonate = str(supplied.get('impersonate') or '').strip()
        requested_kernel = str(supplied.get('kernel') or supplied.get('kernel_label') or '').strip().lower()
        profile = _pick_india_profile(requested_kernel or 'chrome', requested_impersonate)
        major = int(profile['major'])
        build = int(profile.get('build') or 0)
        if build:
            patch = random.randint(*profile['patch_range'])
        else:
            patch = 0
        # kernel 归到 chrome/edge/firefox 三族之一，供日志与 sec-ch-ua-full-version 展示
        self.kernel = str(profile.get('impersonate') or 'chrome').split('_')[0]
        if self.kernel.startswith('chrome'):
            self.kernel = 'chrome'
        elif self.kernel.startswith('edge'):
            self.kernel = 'edge'
        elif self.kernel.startswith('firefox'):
            self.kernel = 'firefox'
        if self.kernel == 'firefox':
            self.chrome_full = f'{major}.0'
        elif self.kernel == 'edge':
            self.chrome_full = str(profile.get('edge') or f'{major}.0.0.0')
        else:
            self.chrome_full = f'{major}.0.{build}.{patch}'
        self.impersonate = str(profile['impersonate'])
        supplied_ua = str(supplied.get('ua') or supplied.get('user_agent') or '').strip()
        ua_version = re.search(r'Chrome/(\d+(?:\.\d+){1,3})', supplied_ua)
        if ua_version and self.kernel == 'chrome':
            self.chrome_full = ua_version.group(1)
        if self.kernel == 'firefox':
            # Firefox 无 sec-ch-ua；sec-ch-ua-platform 留空
            self.ua = supplied_ua if supplied_ua else f'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{major}.0) Gecko/20100101 Firefox/{major}.0'
            self.sec_ch_ua = ''
            self.sec_ch_ua_platform = ''
        elif self.kernel == 'edge':
            self.ua = supplied_ua if supplied_ua else f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36 Edg/{profile.get("edge") or major + ".0.1150.46"}'
            self.sec_ch_ua = str(supplied.get('sec_ch_ua') or profile['sec_ch_ua'])
            self.sec_ch_ua_platform = str(supplied.get('sec_ch_ua_platform') or '\"Windows\"')
        else:
            self.ua = supplied_ua if supplied_ua else f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{self.chrome_full} Safari/537.36'
            self.sec_ch_ua = str(supplied.get('sec_ch_ua') or profile['sec_ch_ua'])
            self.sec_ch_ua_platform = str(supplied.get('sec_ch_ua_platform') or '\"Windows\"')
        # UA 平台与 sentinel/风控上下文必须同源：指纹档案的 platform 字段
        # （Win32/MacIntel）贯穿整次尝试，禁止这里出现 UA 与平台标记打架。
        self.platform = str(supplied.get('platform') or 'Win32')
        self.platform_label = str(supplied.get('platform_os') or ('macOS' if self.platform == 'MacIntel' else 'Windows'))
        try:
            self.hardware_concurrency = int(supplied.get('hardware_concurrency') or 16)
        except (TypeError, ValueError):
            self.hardware_concurrency = 16
        self.max_touch_points = int(supplied.get('max_touch_points') or 0)
        if self.region == 'VN':
            self.accept_language = VN_ACCEPT_LANGUAGE
            self.locale = VN_BROWSER_LOCALE
        else:
            # git5 的语言/时区是该 rail 身份的一部分，固定为 en-IN。不采信指纹里
            # 的值：指纹池在 IN 下还会给出 hi-IN，而且 accept_language 由
            # "locale,base;q=.9,en;q=.8" 拼成，对 en-IN 会退化成
            # "en-IN,en;q=0.9,en;q=0.8"——两者都会和 rail 的其余 HAR 字段矛盾。
            if self.upi_rail == UPI_GIT5_RAIL:
                self.accept_language = UPI_GIT5_ACCEPT_LANGUAGE
                self.locale = UPI_GIT5_BROWSER_LOCALE
            else:
                self.accept_language = str(supplied.get('accept_language') or IN_ACCEPT_LANGUAGE)
                self.locale = str(supplied.get('oai_language') or supplied.get('locale') or IN_BROWSER_LOCALE)
        self.timezone = str(
            supplied.get('timezone')
            or ('Asia/Kolkata' if self.region == 'IN' else 'Asia/Ho_Chi_Minh')
        )
        self.device_id = str(supplied.get('device_id') or uuid.uuid4())
        # Keep one browser identity for the complete UPI attempt. This is a
        # transport/session concern only; it does not change any UPI endpoint,
        # request body, promotion field, or payment state machine.
        self.session_id = str(supplied.get('oai_session_id') or supplied.get('session_id') or uuid.uuid4())
        # Retain the complete per-attempt profile supplied by the distributed
        # worker. The normalized transport fields below are the values used by
        # HTTP headers, while browser bootstrap can still consume screen,
        # platform, hardware and canvas fields from the same identity.
        self.fingerprint = {
            **supplied,
            'user_agent': self.ua,
            'sec_ch_ua': self.sec_ch_ua,
            'sec_ch_ua_mobile': '?0',
            'sec_ch_ua_platform': self.sec_ch_ua_platform,
            'accept_language': self.accept_language,
            'locale': self.locale,
            'timezone': self.timezone,
            'device_id': self.device_id,
            'session_id': self.session_id,
        }
        self.attestation = ''
        self.attestation_source = ''
        # Signed observations and Stripe identifiers are populated only by the
        # real Firefox page opened for this attempt. They are never loaded from
        # environment variables, disk, or another retry.
        self.client_observation = ''
        self.client_version = CHATGPT_CLIENT_VERSION
        self.client_build = CHATGPT_CLIENT_BUILD
        self.browser_sentinel_tokens: dict[str, str] = {}
        self.stripe_request_ids: dict[str, str] = {}
        self.browser_risk_cookie_count = 0
        # UPI 风控上下文（对齐 2026-09-17 印度 UPI.har）：
        #   upi_sentinel_so    —— SO 头按 flow 缓存（建单拿到的 so 在 approve 阶段复用）
        #   attestation_deploy_id —— 从部署证明解出的 deployId，供哨兵 p[6] 使用
        #   client_observation_at —— 观察证明捕获时间，用于按阶段刷新
        self.upi_sentinel_so: dict[str, str] = {}
        # 哨兵 SDK 版本：优先运行期探测（浏览器 bootstrap 读页面 /sentinel/<ver>/sdk.js），
        # 也可用 UPI_SENTINEL_VERSION 显式指定；空则用桥内钉死版本。
        self.sentinel_version = str(os.environ.get('UPI_SENTINEL_VERSION') or '').strip()
        self.attestation_deploy_id = ''
        self.client_observation_at = 0.0
        self.session = _new_upi_http_session(self.impersonate)
        self.session.headers.update(self._browser_headers())
        self.session.cookies.set('oai-did', self.device_id, domain='chatgpt.com')
        self.session.cookies.set('oai-did', self.device_id, domain='.chatgpt.com')
        self.cookie_header = f'oai-did={self.device_id}'
        self._apply_proxy(self.base_proxy)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def _browser_headers(self) -> dict[str, str]:
        fp = self.fingerprint
        headers = {
            'User-Agent': str(fp['user_agent']),
            'Accept-Language': str(fp['accept_language']),
        }
        # Firefox 无 sec-ch-ua 家族；发送空值会与 Firefox 指纹矛盾。
        if self.kernel != 'firefox':
            headers['sec-ch-ua'] = str(fp['sec_ch_ua'])
            headers['sec-ch-ua-mobile'] = str(fp['sec_ch_ua_mobile'])
            headers['sec-ch-ua-platform'] = str(fp['sec_ch_ua_platform'])
            headers['sec-ch-ua-full-version'] = f'"{self.chrome_full}"'
        return headers

    def refresh_cookie_header(self) -> str:
        """Build the explicit ChatGPT Cookie header from this session's jar.

        curl_cffi will maintain its own jar, but the browser checkout also
        sends Cookie explicitly. Keeping one canonical value prevents the
        header and jar from disagreeing after the HTML warmup sets __cf_bm,
        _cfuvid, or other ChatGPT session cookies.
        """
        values: dict[str, str] = {}
        try:
            # curl_cffi's cookie iterator yields cookie *names* (strings), not
            # Cookie objects. get_dict(domain=...) is the portable way to
            # collect the current ChatGPT jar without accidentally forwarding
            # cookies learned from Stripe or another host.
            get_dict = getattr(self.session.cookies, 'get_dict', None)
            if callable(get_dict):
                try:
                    values.update({str(k): str(v) for k, v in get_dict(domain='chatgpt.com').items()})
                except Exception:
                    values.update({str(k): str(v) for k, v in get_dict().items()})
            else:
                for cookie in self.session.cookies:
                    if isinstance(cookie, str):
                        try:
                            values[cookie] = str(self.session.cookies.get(cookie) or '')
                        except Exception:
                            continue
                    else:
                        domain = str(getattr(cookie, 'domain', '') or '').lower()
                        if domain and not domain.endswith('chatgpt.com'):
                            continue
                        name = str(getattr(cookie, 'name', '') or '').strip()
                        if name:
                            values[name] = str(getattr(cookie, 'value', '') or '')
        except Exception:
            values = {}
        # The device cookie is the stable identity anchor and must win over
        # any stale value returned by a previous session.
        values['oai-did'] = self.device_id
        self.cookie_header = '; '.join(f'{key}={value}' for key, value in values.items() if key)
        return self.cookie_header

    def _apply_proxy(self, proxy: str | None) -> str:
        effective = str(proxy or self.base_proxy or '').strip()
        self.proxy = effective
        if effective:
            self.session.proxies.update({'http': effective, 'https': effective})
        else:
            self.session.proxies.clear()
        return effective

    def request(self, method: str, url: str, *, headers: dict[str, str] | None=None, json_body: dict | None=None, form: dict[str, str] | None=None, timeout: float=45.0, allow_redirects: bool=False, proxy: str | None=None) -> tuple[int, str, dict[str, str]]:
        self._apply_proxy(proxy)
        hdrs = dict(headers or {})
        try:
            resp = self.session.request(method.upper(), url, headers=hdrs, json=json_body, data=form, timeout=timeout, allow_redirects=allow_redirects, impersonate=self.impersonate)
        except Exception as exc:
            raise RuntimeError(localize_upi_error(f'Network request failed ({self.impersonate}): {exc}')) from exc
        # Merge response cookies before the next header set is built. The
        # caller still owns the UPI protocol; this only keeps the browser
        # session's Cookie header and curl cookie jar in sync.
        self.refresh_cookie_header()
        if int(resp.status_code) == 403:
            raise RuntimeError(f'UPI risk_blocked: HTTP 403 from {urlsplit(url).netloc}')
        return (resp.status_code, resp.text or '', {k.lower(): v for k, v in resp.headers.items()})

    def request_bytes(self, method: str, url: str, *, headers: dict[str, str] | None=None, timeout: float=45.0, allow_redirects: bool=False, proxy: str | None=None) -> tuple[int, bytes, dict[str, str]]:
        """Fetch binary material without corrupting it through text decoding."""
        self._apply_proxy(proxy)
        try:
            resp = self.session.request(
                method.upper(),
                url,
                headers=dict(headers or {}),
                timeout=timeout,
                allow_redirects=allow_redirects,
                impersonate=self.impersonate,
            )
            self.refresh_cookie_header()
            return (
                resp.status_code,
                bytes(resp.content or b''),
                {k.lower(): v for k, v in resp.headers.items()},
            )
        except Exception as exc:
            raise RuntimeError(localize_upi_error(f'Network request failed ({self.impersonate}): {exc}')) from exc

def _human_pause(kind: str='med') -> None:
    spans = {'short': HUMAN_DELAY_SHORT, 'med': HUMAN_DELAY_MED, 'long': HUMAN_DELAY_LONG, 'handoff': REGION_HANDOFF_DELAY}
    low, high = spans.get(kind, HUMAN_DELAY_MED)
    time.sleep(random.uniform(low, high))

def _upi_promo_page_url(promo_campaign: str = UPI_PROMO_COUPON) -> str:
    campaign = str(promo_campaign or UPI_PROMO_COUPON).strip()
    return f'https://chatgpt.com/?promo_campaign={quote(campaign, safe="")}'


def _warmup_upi_session(client: UpiHttpClient, token: str, page_url: str, proxy: str) -> tuple[int, str]:
    """Warm the same ChatGPT session before risk headers/Checkout.

    This mirrors the GoPay/GCash order: navigate to the campaign page,
    absorb the response cookies, then use that exact Cookie header for all
    subsequent ChatGPT API calls and Sentinel input.
    """
    client._apply_proxy(proxy)
    headers = _chatgpt_headers(client, token)
    headers.update({
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'referer': page_url,
        'cookie': client.refresh_cookie_header(),
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'upgrade-insecure-requests': '1',
    })
    response = client.session.get(page_url, headers=headers, timeout=25)
    client.refresh_cookie_header()
    return int(response.status_code), client.cookie_header


# HAR（2026-09-17 印度 UPI.har）：整份 HAR 里 26 个 ChatGPT 请求都带
#   oai-client-version      = prod-c63495d40cbaaab25a58e53e1207430a35bb2343  ← <html data-build>
#   oai-client-build-number = 10865670                                        ← <html data-seq>
# 两者都能从页面 <html data-build/data-seq> 直接读到（实测线上 chatgpt.com 首页即带）。
# 写死常量会随上游发版立刻变成"旧客户端"，与部署证明 deployId、sentinel p[6] 自相矛盾 ——
# 这是最容易被风控识别为"非真实客户端"的特征之一。
UPI_PAGE_BUILD_RE = re.compile(r'<html[^>]*\bdata-build="([^"]+)"', re.I)
UPI_PAGE_BUILD_ANY_RE = re.compile(r'\bdata-build="([^"]+)"', re.I)
UPI_PAGE_SEQ_RE = re.compile(r'<html[^>]*\bdata-seq="([^"]+)"', re.I)
UPI_PAGE_SEQ_ANY_RE = re.compile(r'\bdata-seq="(\d+)"', re.I)
UPI_PAGE_SEQ_JSON_RE = re.compile(r'"buildNumber"\s*:\s*"?(\d{5,})')


def _sync_upi_client_identity(client: Any) -> None:
    """让 oai-client-version 与部署证明保持一致（deployId 优先）。

    HAR 里 attestation.deployId、sentinel p[6]、oai-client-version 三处恒为同一个
    prod-<hash>；只要拿到部署证明，就用它覆盖客户端版本，避免页面 A/B 变体给出
    与部署证明不同的 data-build。
    """
    deploy_id = _upi_client_deploy_id(client)
    if deploy_id:
        client.client_version = f'prod-{deploy_id}'


def _apply_upi_page_identity(client: Any, html: str) -> None:
    """把页面上的客户端身份写回 client（data-build / data-seq）。"""
    text = str(html or '')
    build_match = UPI_PAGE_BUILD_RE.search(text) or UPI_PAGE_BUILD_ANY_RE.search(text)
    seq_match = (
        UPI_PAGE_SEQ_RE.search(text)
        or UPI_PAGE_SEQ_ANY_RE.search(text)
        or UPI_PAGE_SEQ_JSON_RE.search(text)
    )
    if seq_match:
        seq = str(seq_match.group(1)).strip()
        if seq.isdigit():
            client.client_build = seq
    # 部署证明（服务端为本会话签发）优先；其次才是页面 data-build。
    deploy_id = _attestation_deploy_id(getattr(client, 'attestation', '') or '')
    if deploy_id:
        client.client_version = f'prod-{deploy_id}'
        try:
            setattr(client, 'attestation_deploy_id', deploy_id)
            setattr(client, 'attestation_deploy_id_src', str(getattr(client, 'attestation', '') or ''))
        except Exception:
            pass
    elif build_match:
        build = build_match.group(1).strip()
        if build:
            client.client_version = build if build.startswith('prod-') else f'prod-{build}'


def _fetch_web_attestation(client: UpiHttpClient, page_url: str | None = None) -> str:
    for key in ('PAY153_UPI_ATTESTATION', 'MIN_OAICS_ATTESTATION', 'MIN_GCASH_ATTESTATION'):
        value = str(os.environ.get(key) or '').strip()
        if value and '.' in value:
            return value
    target = str(page_url or 'https://chatgpt.com/')
    try:
        client._apply_proxy(client.proxy)
        response = client.session.get(
            target,
            headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': client.accept_language,
                'User-Agent': client.ua,
                'Origin': 'https://chatgpt.com',
                'Referer': target,
                'oai-device-id': client.device_id,
                'oai-session-id': client.session_id,
                'oai-client-version': str(getattr(client, 'client_version', '') or CHATGPT_CLIENT_VERSION),
                'oai-client-build-number': str(getattr(client, 'client_build', '') or CHATGPT_CLIENT_BUILD),
                'x-openai-web-frontend': 'core_web',
                'x-oai-is-pending-updates': '{"v":3,"updates":[]}',
                'Cookie': client.refresh_cookie_header(),
                'sec-fetch-dest': 'document',
                'sec-fetch-mode': 'navigate',
                'sec-fetch-site': 'same-origin',
                'upgrade-insecure-requests': '1',
            },
            timeout=25,
        )
        client.refresh_cookie_header()
        # 同一份 HTML 里还带客户端身份（data-build / data-seq）→ 同步到 client，
        # 让 oai-client-version / oai-client-build-number 与当前部署一致。
        _apply_upi_page_identity(client, response.text or '')
        # 对齐 gcash_engine/attestation.py:页面 HTML/RSC payload 可能以
        # webDeploymentAttestation":"..." / webDeploymentAttestation=... 形式暴露,
        # 只匹配带前引号的主正则会导致抓取失败(attestation 缺失)。
        match = re.search(r'\"webDeploymentAttestation\"\s*:\s*\"([^\"]+)\"', response.text or '')
        if not match:
            match = re.search(r'[^A-Za-z0-9]webDeploymentAttestation[=:]\s*[\"\']?([A-Za-z0-9._-]+)', response.text or '')
        return match.group(1).strip() if match and '.' in match.group(1) else ''
    except Exception:
        return ''


def _load_upi_sentinel_runtime():
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'gcash_engine', 'sentinel_runtime.py'))
    spec = importlib.util.spec_from_file_location('_upi_sentinel_runtime', path)
    if not spec or not spec.loader:
        raise RuntimeError('UPI 浏览器风控模块不可用')
    module = importlib.util.module_from_spec(spec)
    # dataclass 会在装饰阶段通过 sys.modules 查找当前模块。
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _upi_firefox_executable() -> str:
    """Find Firefox for the live UPI page; blank lets Playwright use its bundle."""
    candidates: list[str] = []
    configured = str(os.environ.get('UPI_FIREFOX_EXECUTABLE') or os.environ.get('FIREFOX_PATH') or '').strip()
    if configured:
        candidates.append(configured)
    if os.name == 'nt':
        local_app_data = str(os.environ.get('LOCALAPPDATA') or '').strip()
        candidates.extend((
            r'C:\Program Files\Mozilla Firefox\firefox.exe',
            r'C:\Program Files (x86)\Mozilla Firefox\firefox.exe',
        ))
        if local_app_data:
            candidates.append(str(Path(local_app_data) / 'Mozilla Firefox' / 'firefox.exe'))
    candidates.extend((
        '/usr/bin/firefox',
        '/usr/bin/firefox-esr',
    ))
    return next((candidate for candidate in candidates if Path(candidate).is_file()), '')


def _merge_browser_cookies(client: UpiHttpClient, cookies: Any) -> None:
    if not isinstance(cookies, dict):
        return
    for name, value in cookies.items():
        name = str(name or '').strip()
        value = str(value or '').strip()
        if not name or not value or '\r' in value or '\n' in value:
            continue
        try:
            client.session.cookies.set(name, value, domain='chatgpt.com')
        except Exception:
            pass
    client.refresh_cookie_header()


def _upi_browser_proxy(proxy: str) -> tuple[str, Any]:
    """把带认证 SOCKS5 转成本机 HTTP 桥，供 Chromium/Playwright 使用。"""
    parsed = urlsplit(str(proxy or ''))
    if parsed.scheme not in ('socks5', 'socks5h') or not (parsed.username or parsed.password):
        return str(proxy or ''), None
    gcash_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'gcash_engine'))
    import sys
    if gcash_dir not in sys.path:
        sys.path.insert(0, gcash_dir)
    try:
        from gcash_custom_flow import _proxy_for_playwright  # type: ignore
        holder: list[Any] = []
        config = _proxy_for_playwright(str(proxy or ''), holder)
        server = str((config or {}).get('server') or '')
        bridge = holder[0] if holder else None
        if not server or bridge is None:
            raise RuntimeError('未能启动 SOCKS5 浏览器代理桥')
        return server, bridge
    except Exception as exc:
        raise RuntimeError(f'UPI 浏览器代理桥启动失败: {str(exc)[:160]}') from exc


def _browser_risk_enabled() -> bool:
    """上一代 UPI 链路要求真实 Firefox 风控上下文；git5 不再需要。"""
    return True


def _capture_upi_browser_risk(
    client: UpiHttpClient,
    *,
    access_token: str,
    proxy: str,
    page_url: str,
    flow: str,
) -> dict[str, Any]:
    """按 rail 分派风控上下文的获取方式。

    upi-git5 是源站的「纯协议 · 无浏览器内核」通道：它不打开页面，而是在本地
    按 HAR 形状构造部署证明、观察证明和 Stripe 标识。其余 rail 保持原有行为，
    由真实 Firefox 页面捕获这些凭据。

    保持本函数名不变，是为了让两条路径共用同一个可 patch 的接缝——现有测试
    都是 patch 这个符号的。
    """
    if _upi_protocol_client(client):
        return _capture_upi_protocol_risk(
            client, access_token=access_token, proxy=proxy, page_url=page_url, flow=flow
        )
    return _capture_upi_firefox_risk(
        client, access_token=access_token, proxy=proxy, page_url=page_url, flow=flow
    )


def _capture_upi_protocol_risk(
    client: UpiHttpClient,
    *,
    access_token: str,
    proxy: str,
    page_url: str,
    flow: str,
) -> dict[str, Any]:
    """源站 upi-git5 的协议风控上下文：不启动任何浏览器。

    与 Firefox 版本的差异集中在这里：部署证明改为直接 HTTP 抓 ChatGPT 页面，
    观察证明与 Stripe guid/muid/sid 按 HAR 形状本地生成。返回结构与
    `_capture_upi_firefox_risk` 完全同构，调用方的 gate 逻辑无需分叉。
    """
    # 部署证明：复用已有的无浏览器抓取（页面 HTML 里的 webDeploymentAttestation）。
    attestation = str(getattr(client, 'attestation', '') or '').strip()
    if not attestation:
        attestation = _fetch_web_attestation(client, page_url)
    if attestation:
        client.attestation = attestation
        client.attestation_source = UPI_PROTOCOL_ATTESTATION_SOURCE
    else:
        client.attestation = ''
        client.attestation_source = ''
    _sync_upi_client_identity(client)

    # HAR：x-oai-is-client-observation 是阶段级值，每次捕获都轮换。
    client.client_observation = 'v1.r.p.' + secrets.token_urlsafe(12)[:16]
    client.client_observation_at = time.monotonic()

    # Stripe 四个请求标识：源站在 macOS rail 上用 _gen_fingerprint() 生成，形状
    # 与浏览器 __stripe_mid/__stripe_sid 一致。已有值保留（与源站的 or-merge 一致），
    # 保证同一次尝试内 guid/muid/sid 稳定。
    existing = client.stripe_request_ids if isinstance(client.stripe_request_ids, dict) else {}
    client.stripe_request_ids = {
        'guid': str(existing.get('guid') or uuid.uuid4().hex),
        'muid': str(existing.get('muid') or uuid.uuid4().hex),
        'sid': str(existing.get('sid') or uuid.uuid4().hex),
        'stripe_js_id': str(existing.get('stripe_js_id') or uuid.uuid4()),
    }

    cookie_header = client.refresh_cookie_header()
    cookie_count = len([item for item in str(cookie_header or '').split(';') if item.strip()])
    client.browser_risk_cookie_count = cookie_count
    client.identity_source = f'macos_{client.impersonate}_protocol'
    client.runtime_source = 'har_macos_chrome136'

    return {
        'attestation': bool(client.attestation),
        'attestation_length': len(client.attestation),
        'attestation_source': client.attestation_source or 'none',
        'sentinel': False,
        'sentinel_length': 0,
        'observation': True,
        'browser_cookies': bool(cookie_count),
        'browser_cookie_count': cookie_count,
        'stripe_ids': all(
            client.stripe_request_ids.get(key) for key in ('guid', 'muid', 'sid', 'stripe_js_id')
        ),
        'protocol': True,
        'error': '',
    }


def _upi_risk_context_ok(client: Any, risk: dict[str, Any]) -> bool:
    """风控上下文是否达到该 rail 的最低要求。

    协议模式（git5）刻意不要求部署证明：源站 macOS 上下文里根本没有
    oai_web_deployment_attestation，其请求头组装会过滤掉空值（app.py:1168），
    所以 git5 是允许"无 attestation 直连"的。浏览器 rail 仍然 fail-closed。
    """
    if not str(getattr(client, 'client_observation', '') or '').strip():
        return False
    if _upi_protocol_client(client):
        return True
    return bool(
        getattr(client, 'attestation', '')
        and str(risk.get('attestation_source') or '').lower() in UPI_ATTESTATION_SOURCES
        and risk.get('browser_cookies')
    )


def _capture_upi_firefox_risk(
    client: UpiHttpClient,
    *,
    access_token: str,
    proxy: str,
    page_url: str,
    flow: str,
) -> dict[str, Any]:
    """从当前真实浏览器部署捕获 attestation，并缓存同设备的 Sentinel。

    这里只返回存在性、长度和来源等安全诊断信息，不记录任何令牌内容。
    """
    runtime = _load_upi_sentinel_runtime()
    executable = _upi_firefox_executable()
    cookies = runtime.normalize_cookie_mapping(client.refresh_cookie_header())
    fp = {
        **(client.fingerprint if isinstance(getattr(client, 'fingerprint', None), dict) else {}),
        'ua': client.ua,
        'user_agent': client.ua,
        'locale': client.locale,
        'oai_language': client.locale,
        'timezone': client.timezone,
        'platform': client.platform,
        'platform_os': client.platform_label,
        'device_id': client.device_id,
        'oai_session_id': client.session_id,
    }
    browser_proxy = proxy
    proxy_bridge = None
    try:
        browser_proxy, proxy_bridge = _upi_browser_proxy(proxy)
        bootstrap = runtime.capture_browser_bootstrap(
            access_token=access_token,
            session_token='',
            cookies=cookies,
            proxy=browser_proxy,
            device_id=client.device_id,
            session_id=client.session_id,
            fingerprint=fp,
            executable_path=executable,
            browser_type='firefox',
            page_url=page_url,
            sentinel_flow=flow,
            timeout_s=25.0,
        )
    finally:
        if proxy_bridge is not None:
            try:
                proxy_bridge.close()
            except Exception:
                pass
    browser_cookies = getattr(bootstrap, 'browser_cookies', {})
    _merge_browser_cookies(client, browser_cookies)
    client.browser_risk_cookie_count = len(browser_cookies) if isinstance(browser_cookies, dict) else 0
    attestation_source = str(getattr(bootstrap, 'source', '') or '').strip().lower()
    attestation = str(getattr(bootstrap, 'attestation', '') or '').strip()
    if (
        attestation_source in {'browser', 'browser_hydration'}
        and attestation
        and runtime.attestation_is_current(attestation)
    ):
        client.attestation = attestation
        client.attestation_source = attestation_source
    else:
        client.attestation = ''
        client.attestation_source = ''
    observation = str(getattr(bootstrap, 'observation', '') or '').strip()
    if observation.startswith('v1.'):
        client.client_observation = observation
        # HAR：该值按阶段轮换（同一份 HAR 里 6 个不同值），记录捕获时间用于刷新判断。
        client.client_observation_at = time.monotonic()
    else:
        client.client_observation = ''
        client.client_observation_at = 0.0
    client_version = str(getattr(bootstrap, 'client_version', '') or '').strip()
    client_build = str(getattr(bootstrap, 'client_build', '') or '').strip()
    if client_version:
        client.client_version = client_version
    if client_build:
        client.client_build = client_build
    # GCash 同源的运行期探测：页面里 /sentinel/<ver>/sdk.js 的版本号，
    # 用来让桥对外宣称的 SDK 版本与线上一致（UPI 之前一直用钉死的版本）。
    sentinel_version = str(getattr(bootstrap, 'sentinel_version', '') or '').strip()
    if sentinel_version:
        client.sentinel_version = sentinel_version
    sentinel_token = str(getattr(bootstrap, 'sentinel_token', '') or '').strip()
    if sentinel_token:
        client.browser_sentinel_tokens[flow] = sentinel_token
    stripe_ids = getattr(bootstrap, 'stripe_ids', {})
    if isinstance(stripe_ids, dict):
        captured_ids = {
            key: str(stripe_ids.get(key) or '').strip()
            for key in ('guid', 'muid', 'sid', 'stripe_js_id')
            if str(stripe_ids.get(key) or '').strip()
        }
        client.stripe_request_ids = captured_ids
    return {
        'attestation': bool(client.attestation),
        'attestation_length': len(client.attestation),
        'attestation_source': client.attestation_source or 'none',
        'sentinel': bool(sentinel_token),
        'sentinel_length': len(sentinel_token),
        'observation': bool(client.client_observation),
        'browser_cookies': bool(browser_cookies),
        'browser_cookie_count': len(browser_cookies) if isinstance(browser_cookies, dict) else 0,
        'stripe_ids': all(client.stripe_request_ids.get(key) for key in ('guid', 'muid', 'sid', 'stripe_js_id')),
        'error': str(getattr(bootstrap, 'error', '') or '')[:180],
    }


# HAR：x-oai-is-client-observation 是"阶段级"值——同一份 HAR 里出现 6 个不同取值
# （Ru0_…→IEsk…→L-Ngwz…→vecOhRy…→kwzi…→goZe…），覆盖建单/税费/快照/审批；
# 而 oai-web-deployment-attestation 全程只有一个值（1 小时有效）。
# 因此观察证明超过窗口就重抓一次，部署证明沿用即可。
UPI_OBSERVATION_MAX_AGE_S = float(str(os.environ.get('UPI_OBSERVATION_MAX_AGE_S') or '90') or 90)


def _upi_observation_stale(client: UpiHttpClient, max_age_s: float = 0.0) -> bool:
    """观察证明是否缺失/过期（布尔占位值一律按过期处理）。"""
    value = str(getattr(client, 'client_observation', '') or '').strip()
    if not value or value == 'true':
        return True
    stamp = float(getattr(client, 'client_observation_at', 0.0) or 0.0)
    if not stamp:
        return True
    limit = float(max_age_s or UPI_OBSERVATION_MAX_AGE_S)
    return (time.monotonic() - stamp) > limit


def _upi_refresh_browser_risk(
    client: UpiHttpClient,
    *,
    access_token: str,
    proxy: str,
    page_url: str,
    flow: str = '',
    max_age_s: float = 0.0,
    step: Any = None,
    stage: str = '风控上下文',
) -> dict[str, Any]:
    """Refresh when stale, then fail closed unless the full live context is valid."""
    risk: dict[str, Any] = {}
    if _upi_protocol_client(client):
        # 协议模式不"刷新浏览器上下文"，而是按阶段重铸观察证明——源站 git5 在
        # 建单/税费/快照/审批各阶段都会换一个 v1.r.p.… 值。因此这里跳过
        # _upi_observation_stale 的 90 秒窗口判断，直接重铸。
        risk = _capture_upi_protocol_risk(
            client,
            access_token=access_token,
            proxy=proxy,
            page_url=page_url,
            flow=flow,
        )
        if callable(step):
            step(
                stage,
                'ok',
                'macOS 纯协议风控上下文：部署证明='
                + ('已获取' if client.attestation else '未获取（沿用无证明直连）')
                + '；观察证明=已轮换'
                + '；Stripe ID=' + ('4/4' if risk.get('stripe_ids') else '不完整'),
            )
        return risk
    if _upi_observation_stale(client, max_age_s):
        risk = _capture_upi_browser_risk(
            client,
            access_token=access_token,
            proxy=proxy,
            page_url=page_url,
            flow=flow,
        )
    source = str(
        risk.get('attestation_source')
        or getattr(client, 'attestation_source', '')
        or ''
    ).strip().lower()
    cookie_count = int(
        risk.get('browser_cookie_count')
        or getattr(client, 'browser_risk_cookie_count', 0)
        or 0
    )
    try:
        _locked_stripe_request_ids(client)
        stripe_ids_ok = True
    except RuntimeError:
        stripe_ids_ok = False
    if (
        not client.attestation
        or source not in UPI_ATTESTATION_SOURCES
        or not client.client_observation
        or cookie_count <= 0
        or not stripe_ids_ok
    ):
        detail = str(risk.get('error') or '').strip()
        raise RuntimeError(
            'UPI 风控上下文生成失败：当前浏览器会话缺少 attestation、'
            'signed observation、Cookie 或 Stripe guid/muid/sid/stripe_js_id'
            + (f'; {detail}' if detail else '')
        )
    if callable(step):
        step(
            stage,
            'ok',
            '动态部署证明=' + ('已获取' if risk.get('attestation') else '未获取')
            + f"（来源={source or '未知'}，长度={len(client.attestation)}）"
            + '；客户端观察证明=' + ('已刷新' if risk.get('observation') else '仍在90秒有效期内'),
        )
    return risk


def _chatgpt_headers(client: UpiHttpClient, token: str, *, promotion: bool=False) -> dict[str, str]:
    accept_language = VN_ACCEPT_LANGUAGE if promotion else client.accept_language
    browser_locale = VN_BROWSER_LOCALE if promotion else client.locale
    # 发头前对齐客户端身份：deployId 存在时 oai-client-version 必须等于 prod-<deployId>
    _sync_upi_client_identity(client)
    account_id = _jwt_account_id(token)
    headers = {
        'user-agent': client.ua,
        'accept': '*/*',
        'accept-language': accept_language,
        'authorization': f'Bearer {token}',
        'origin': 'https://chatgpt.com',
        'referer': 'https://chatgpt.com/',
        'content-type': 'application/json',
        'oai-device-id': client.device_id,
        'oai-language': browser_locale,
        # GoPay/GCash use a stable session id throughout one browser flow.
        'oai-session-id': client.session_id,
        'oai-client-version': client.client_version,
        'oai-client-build-number': client.client_build,
        # 遥测头：按 HAR 结构动态生成（静态 [1,null] 是机器人特征，见 _upi_telemetry）
        'oai-telemetry': _upi_telemetry(client),
        # HAR #12/#228 等 ChatGPT 请求固定携带的三个客户端标记头（缺失即"非真实前端"）
        'x-openai-web-frontend': 'core_web',
        'x-openai-codex-window-type': 'not_applicable',
        'x-oai-is-pending-updates': '{"v":3,"updates":[]}',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'cookie': client.cookie_header or f'oai-did={client.device_id}',
    }
    # Firefox 无 sec-ch-ua 家族；不发送空 sec-ch-ua 头，保持与 UA/TLS 指纹一致。
    if client.kernel != 'firefox':
        headers['sec-ch-ua'] = client.sec_ch_ua
        headers['sec-ch-ua-mobile'] = '?0'
        headers['sec-ch-ua-platform'] = client.sec_ch_ua_platform
    if account_id:
        headers['Chatgpt-Account-Id'] = account_id
    if client.attestation:
        headers['oai-web-deployment-attestation'] = client.attestation
    if client.client_observation:
        # 该值来自当前 Chromium 部署，不能用固定字符串代替。
        headers['x-oai-is-client-observation'] = client.client_observation
    return headers

def _spawn_region_client(base_proxy: str, region: str, *, fresh_sid: bool=False, fingerprint: dict[str, Any] | None = None) -> UpiHttpClient:
    region = str(region or 'IN').upper()
    if region == 'VN':
        proxy = resolve_upi_promotion_proxy(base_proxy)
    else:
        proxy = proxy_for_region(base_proxy, region) or base_proxy
        if fresh_sid:
            proxy = _proxy_with_fresh_sid(proxy)
    return UpiHttpClient(proxy, region=region, fingerprint=fingerprint)

def _warmup_india_client(client: UpiHttpClient, token: str, proxy: str) -> None:
    headers = _chatgpt_headers(client, token)
    account_id = _jwt_account_id(token)
    paths = [
        '/backend-api/checkout_pricing_config/configs/IN',
        '/backend-api/subscriptions/has_app_store_subscription_in_billing_retry',
    ]
    # HAR #1：真实客户端在建单前先读一次可用支付方式（同一次响应还带出
    # one_click_trial_eligible，与优惠资格相关），请求头只带 target-path/route，
    # query 里的 account_id 不进 target 头。
    if account_id:
        paths.insert(0, f'/backend-api/payments/payment_methods?account_id={account_id}')
    for path in paths:
        route = path.split('?', 1)[0]
        try:
            client.request(
                'GET',
                'https://chatgpt.com' + path,
                headers=_target_headers(headers, route, 'https://chatgpt.com/'),
                proxy=proxy,
                timeout=25.0,
            )
        except Exception:
            pass

def _target_headers(base: dict[str, str], path: str, referer: str) -> dict[str, str]:
    hdrs = dict(base)
    hdrs['referer'] = referer
    hdrs['x-openai-target-path'] = path
    hdrs['x-openai-target-route'] = path
    return hdrs

def _sentinel_ping(client: UpiHttpClient, token: str, proxy: str, referer: str) -> None:
    headers = _target_headers(_chatgpt_headers(client, token), '/backend-api/sentinel/ping', referer)
    try:
        client.request('POST', 'https://chatgpt.com/backend-api/sentinel/ping', headers=headers, json_body={}, proxy=proxy, timeout=25.0)
    except Exception:
        pass

# stripe.js 运行时版本（HAR confirm 的 version / payment_user_agent 就是它）：
# 该值等于 stripe.js 内部的 STRIPE_JS_BUILD_SALT，上游每次发版都会变；钉死常量
# 会立刻落后，所以这里直接从同一个脚本解析，进程内缓存 6 小时。
_STRIPE_RUNTIME_VERSION_CACHE: dict[str, Any] = {'value': '', 'at': 0.0}
_STRIPE_RUNTIME_VERSION_TTL_S = 6 * 3600
_STRIPE_BUILD_SALT_RE = re.compile(r'STRIPE_JS_BUILD_SALT\s+([0-9a-z]{6,})')
_STRIPE_RUNTIME_VERSION_URLS = (
    'https://js.stripe.com/basil/stripe.js',
    'https://js.stripe.com/v3/fingerprinted/js/shared-00e8886af426ce0f77144909fd2a839b.js',
)


def _fetch_stripe_runtime_version(client: UpiHttpClient, proxy: str = '') -> str:
    """解析当前 stripe.js 的运行时版本；失败返回空串（调用方回退常量）。"""
    cached = str(_STRIPE_RUNTIME_VERSION_CACHE.get('value') or '').strip()
    stamp = float(_STRIPE_RUNTIME_VERSION_CACHE.get('at') or 0.0)
    if cached and (time.monotonic() - stamp) < _STRIPE_RUNTIME_VERSION_TTL_S:
        return cached
    for url in _STRIPE_RUNTIME_VERSION_URLS:
        try:
            status, text, _ = client.request('GET', url, headers=_stripe_headers(client), proxy=proxy, timeout=25.0)
        except Exception:
            continue
        if status != 200 or not text:
            continue
        match = _STRIPE_BUILD_SALT_RE.search(str(text))
        if match:
            value = match.group(1).strip()
            _STRIPE_RUNTIME_VERSION_CACHE['value'] = value
            _STRIPE_RUNTIME_VERSION_CACHE['at'] = time.monotonic()
            return value
    return ''


def _stripe_runtime_version(client: UpiHttpClient, proxy: str = '', fallback: str = '') -> str:
    """运行时版本解析顺序：环境变量 → 实时抓取 → 兜底常量。"""
    env_value = str(os.environ.get('PAY153_UPI_STRIPE_RUNTIME_VERSION') or '').strip()
    if env_value:
        return env_value
    live = _fetch_stripe_runtime_version(client, proxy)
    if live:
        return live
    return str(fallback or '').strip()


# 浏览器上报的 IANA 时区名与 Stripe 侧记录值：Chrome 对印度返回 "Asia/Calcutta"，
# 与 HAR 的 browser_timezone 一致；这里做一次等价别名归一。
_STRIPE_TIMEZONE_ALIASES = {
    'Asia/Kolkata': 'Asia/Calcutta',
    'Asia/Ho_Chi_Minh': 'Asia/Saigon',
}


def _stripe_browser_locale(client: Any) -> str:
    """元素/支付页请求里的 locale，必须与本会话指纹一致（HAR: hi-IN）。"""
    value = str(getattr(client, 'locale', '') or '').strip()
    return value or IN_BROWSER_LOCALE


def _stripe_browser_timezone(client: Any) -> str:
    """元素/支付页请求里的 browser_timezone（HAR: Asia/Calcutta）。"""
    value = str(getattr(client, 'timezone', '') or '').strip()
    if not value:
        region = str(getattr(client, 'region', '') or 'IN').upper()
        value = 'Asia/Kolkata' if region == 'IN' else 'Asia/Ho_Chi_Minh'
    return _STRIPE_TIMEZONE_ALIASES.get(value, value)


def _upi_telemetry(client: Any) -> str:
    """oai-telemetry 头：[1,<elapsed>,a,b,c,2,0,d]。

    HAR #12 实测形如 [1,801.3999999985099,8,64,42,2,0,805]；PIX 渠道的同款实现
    （pix_service._oai_telemetry）已确认静态 "[1,null]" 是明显的机器人特征。
    这里比 PIX 再收敛一步：elapsed 用本会话真实经过时间、d≈elapsed（与 HAR 一致）、
    计数器随本会话请求数递增，同一会话内不会出现自相矛盾的数值。
    """
    now = time.monotonic()
    started = float(getattr(client, 'telemetry_started_at', 0.0) or 0.0)
    if not started:
        started = now
        try:
            setattr(client, 'telemetry_started_at', started)
        except Exception:
            pass
    seq = int(getattr(client, 'telemetry_seq', 0) or 0) + 1
    try:
        setattr(client, 'telemetry_seq', seq)
    except Exception:
        pass
    elapsed = round(max(120.0, (now - started) * 1000.0) + random.uniform(0.0, 40.0), 4)
    counter_b = int(getattr(client, 'telemetry_counter_b', 0) or 0)
    counter_c = int(getattr(client, 'telemetry_counter_c', 0) or 0)
    if not counter_b or not counter_c:
        counter_b = random.randint(30, 240)
        counter_c = random.randint(10, 90)
        try:
            setattr(client, 'telemetry_counter_b', counter_b)
            setattr(client, 'telemetry_counter_c', counter_c)
        except Exception:
            pass
    total = round(elapsed + random.uniform(0.0, 8.0), 1)
    return f'[1,{elapsed},{min(seq, 60)},{counter_b},{counter_c},2,0,{total}]'


def _stripe_headers(client: UpiHttpClient) -> dict[str, str]:
    return {'user-agent': client.ua, 'accept-language': client.accept_language, 'origin': 'https://js.stripe.com', 'referer': 'https://js.stripe.com/', 'sec-ch-ua': client.sec_ch_ua, 'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': client.sec_ch_ua_platform}

def _load_upi_sentinel_module():
    """加载 UPI 私有哨兵模块 upi_sentinel.py，缺文件时回退共享实现。

    UPI 需要 p[6]=deployId、p[15]=页面查询参数名 这类 HAR 对齐字段，只存在于
    UPI 私有桥；共享实现保留为兜底，保证任何部署缺文件时链路仍能运行。
    """
    here = os.path.dirname(__file__)
    candidates = (
        ('_upi_local_sentinel', os.path.abspath(os.path.join(here, 'upi_sentinel.py'))),
        ('_upi_gcash_sentinel', os.path.abspath(os.path.join(here, '..', '..', 'gcash_engine', 'sentinel_token.py'))),
    )
    last_error: Exception | None = None
    for name, path in candidates:
        if not os.path.exists(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if not spec or not spec.loader:
                raise RuntimeError(f'spec unavailable: {path}')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if callable(getattr(module, 'mint_sentinel_sync', None)):
                return module
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f'UPI Sentinel bridge module unavailable: {last_error}')


def _attestation_deploy_id(attestation: str) -> str:
    """从 oai-web-deployment-attestation 的 JWT payload 解出 deployId。

    HAR 证据：哨兵 p[6] = "prod-c63495d40cbaaab25a58e53e1207430a35bb2343"，与同一份
    HAR 里部署证明 payload 的 deployId 完全同值 —— 两处必须同源。
    """
    raw = str(attestation or '').strip()
    if not raw or '.' not in raw:
        return ''
    parts = [item for item in raw.split('.') if item]
    if not parts:
        return ''
    # 实测格式是两段 "<base64url(payload)>.<base64url(signature)>"（没有 JWT header），
    # 也兼容三段 JWT；逐个候选段尝试，取第一个含 deployId 的 JSON。
    candidates = [parts[1], parts[0]] if len(parts) >= 3 else [parts[0]]
    for candidate in candidates:
        text = candidate + '=' * (-len(candidate) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(text).decode('utf-8', 'replace'))
        except Exception:
            continue
        if isinstance(payload, dict):
            deploy_id = str(payload.get('deployId') or '').strip()
            if deploy_id:
                return deploy_id
    return ''


def _upi_client_deploy_id(client: Any) -> str:
    """取当前 client 的部署 id（解出一次后缓存到 client）。

    来源优先级：部署证明 payload 的 deployId > 页面 data-build（oai-client-version）
    里的哈希。两者本来就是同一个值，HAR 里 attestation.deployId、sentinel p[6]、
    oai-client-version 三处必须一致。
    """
    attestation = str(getattr(client, 'attestation', '') or '').strip()
    cached = str(getattr(client, 'attestation_deploy_id', '') or '').strip()
    cached_src = str(getattr(client, 'attestation_deploy_id_src', '') or '')
    if cached and attestation and attestation == cached_src:
        return cached
    deploy_id = _attestation_deploy_id(attestation)
    if not deploy_id:
        version = str(getattr(client, 'client_version', '') or '').strip()
        if version.startswith('prod-') and len(version) > 5:
            deploy_id = version[5:].strip()
    if deploy_id:
        try:
            setattr(client, 'attestation_deploy_id', deploy_id)
            setattr(client, 'attestation_deploy_id_src', attestation)
        except Exception:
            pass
    return deploy_id


def _page_param_name(page_url: str) -> str:
    """页面 URL 的第一个查询参数名（HAR 的 p[15]，例如 promo_campaign）。"""
    try:
        query = urlsplit(str(page_url or '')).query
    except Exception:
        return ''
    for pair in str(query or '').split('&'):
        name = pair.split('=', 1)[0].strip()
        if name:
            return name
    return ''


def _upi_sentinel_so_cache(client: Any) -> dict[str, str]:
    cache = getattr(client, 'upi_sentinel_so', None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            setattr(client, 'upi_sentinel_so', cache)
        except Exception:
            pass
    return cache


def _upi_apply_sentinel_so(headers: dict[str, str], flow: str, so_value: str, so_cache: dict[str, str]) -> dict[str, str]:
    """按 HAR 的时机挂 openai-sentinel-so-token。

    HAR #12（payments/checkout，flow=chatgpt_checkout）只带 sentinel 主令牌；
    HAR #228（approve）才带 SO，而且 SO 内部的 flow 仍是 chatgpt_checkout ——
    说明 approve 复用建单阶段那份 SO（approve 自己的 sentinel/req 响应不下发 so）。
    因此：建单阶段只缓存不发，approve 阶段优先用本次新 SO，缺失则复用建单缓存。
    """
    if so_value:
        so_cache[flow] = so_value
    if flow == 'chatgpt_checkout':
        return headers
    value = str(so_cache.get(flow) or so_cache.get('chatgpt_checkout') or '').strip()
    if value:
        headers['OpenAI-Sentinel-SO-Token'] = value
    return headers


def _upi_sentinel_headers(proxy: str, device_id: str, flow: str = 'checkout_session_approval', client: UpiHttpClient | None = None, page_url: str | None = None) -> dict[str, str]:
    """用 UPI 私有哨兵桥生成 openai-sentinel-token / SO（对齐印度 UPI.har）。

    * flow：建单 chatgpt_checkout，approve/confirm checkout_session_approval；
    * p[6] deployId 取自当前 client 的部署证明，p[15] 取自页面 URL；
    * SO 头只在 approve 阶段发送，建单阶段仅缓存（与 HAR 时机一致）。
    """
    module = _load_upi_sentinel_module()
    active = client
    if active is None:
        active = UpiHttpClient(proxy, region='IN')
        owned = True
    else:
        owned = False
    try:
        so_cache = _upi_sentinel_so_cache(active)
        # 缓存里的令牌来自浏览器页面。协议模式没有这一步，且源站 git5 每个阶段
        # 都重新铸造，所以协议模式下既不取也不写这份缓存。
        protocol = _upi_protocol_client(active)
        if not protocol:
            cached = str(active.browser_sentinel_tokens.pop(flow, '') or '').strip()
            if cached:
                return _upi_apply_sentinel_so({'openai-sentinel-token': cached}, flow, '', so_cache)
        cookie_header = active.refresh_cookie_header()
        mint_kwargs = dict(
            flow=flow,
            device_id=device_id,
            user_agent=active.ua,
            proxy=proxy,
            page_url=str(page_url or 'https://chatgpt.com/'),
            language=active.locale,
            timezone=getattr(active, 'timezone', '') or ('Asia/Kolkata' if active.region == 'IN' else 'Asia/Ho_Chi_Minh'),
            # 哨兵桥入参必须与本次请求指纹同源：平台/屏幕/触控/并发全部取
            # 自指纹档案（默认 Win32/1920x1080/无触控，与 curl_cffi 桌面指纹一致），
            # 禁止 UA 是 macOS 而哨兵上下文仍是 Win32 的自相矛盾。
            cores=getattr(active, 'hardware_concurrency', 16) or 16,
            platform=getattr(active, 'platform', 'Win32') or 'Win32',
            platform_label=getattr(active, 'platform_label', 'Windows') or 'Windows',
            screen_w=1920,
            screen_h=1080,
            max_touch_points=getattr(active, 'max_touch_points', 0) or 0,
            cookie_header=cookie_header,
            # MoMo/GCash use a bounded 60s bridge call. Keeping UPI at the
            # same ceiling prevents a stalled proxy from holding a worker for
            # 90s before the outer retry/rotation can take over.
            timeout_s=60.0,
        )
        # UPI 私有桥额外接受 deployId / pageParam；共享兜底实现不接受这两个参数。
        # 用签名判断而不是 try/except TypeError，避免把桥内部的真实错误吞掉。
        mint_fn = module.mint_sentinel_sync
        accepts_extra = False
        accepts_version = False
        try:
            params = inspect.signature(mint_fn).parameters
            accepts_extra = 'deploy_id' in params or any(
                item.kind == inspect.Parameter.VAR_KEYWORD for item in params.values()
            )
            accepts_version = 'sentinel_version' in params or any(
                item.kind == inspect.Parameter.VAR_KEYWORD for item in params.values()
            )
        except (TypeError, ValueError):
            accepts_extra = False
            accepts_version = False
        extra_kwargs: dict[str, Any] = {}
        if accepts_extra:
            extra_kwargs['deploy_id'] = _upi_client_deploy_id(active)
            extra_kwargs['page_param'] = _page_param_name(page_url or '')
        if accepts_version:
            extra_kwargs['sentinel_version'] = str(getattr(active, 'sentinel_version', '') or '').strip()
        token = mint_fn(**extra_kwargs, **mint_kwargs)
        value = module.sentinel_header_value(token)
        if not value:
            # Match MoMo/GCash graceful fallback: a bridge timeout/empty
            # result must not abort an otherwise valid Checkout session. The
            # upstream risk layer can still accept the attestation/context or
            # return a classified 403/blocked response for the outer retry.
            return {}
        out = {'openai-sentinel-token': value}
        so_builder = getattr(module, 'so_header_value', None)
        so_value = so_builder(token.get('_so')) if callable(so_builder) and isinstance(token, dict) else ''
        return _upi_apply_sentinel_so(out, flow, so_value, so_cache)
    finally:
        if owned:
            active.close()

def _load_upi_hcaptcha_module():
    """加载 freepp-backend 已投产的 Stripe hCaptcha passive 求解器。"""
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'freepp-backend', 'core', 'hcaptcha_chrome.py'))
    spec = importlib.util.spec_from_file_location('_upi_hcaptcha_chrome', path)
    if not spec or not spec.loader:
        raise RuntimeError('UPI hCaptcha 求解模块不可用')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_upi_hcaptcha_protocol_module():
    """加载 PayPal passive PoW 协议实现的 Stripe 通用适配。"""
    path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), '..', 'freepp-backend', 'core',
            'hcaptcha_passive_protocol.py',
        )
    )
    spec = importlib.util.spec_from_file_location('_upi_hcaptcha_passive_protocol', path)
    if not spec or not spec.loader:
        raise RuntimeError('UPI hCaptcha passive 协议模块不可用')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client_cookies_map(client: Any) -> dict[str, str]:
    """收集当前会话的 cookie 表，兼容 curl_cffi 迭代返回 cookie 名（字符串）的情况。

    refresh_cookie_header 已经说明 curl_cffi 的 cookie 迭代器产出的是 cookie 名
    （字符串）而非 Cookie 对象；这里按同样约定收敛成一个 {name: value} 映射，
    供 hCaptcha Chrome 兜底把当前会话 cookie 注入真实浏览器。
    """
    if client is None:
        return {}
    session = getattr(client, 'session', None)
    if session is None or getattr(session, 'cookies', None) is None:
        return {}
    values: dict[str, str] = {}
    try:
        get_dict = getattr(session.cookies, 'get_dict', None)
        if callable(get_dict):
            try:
                values.update({str(k): str(v) for k, v in get_dict().items()})
            except Exception:
                pass
        else:
            for cookie in session.cookies:
                if isinstance(cookie, str):
                    try:
                        values[cookie] = str(session.cookies.get(cookie) or '')
                    except Exception:
                        continue
                else:
                    name = str(getattr(cookie, 'name', '') or '').strip()
                    if name:
                        values[name] = str(getattr(cookie, 'value', '') or '')
    except Exception:
        pass
    return values


def _upi_passive_captcha_fields(
    proxy: str,
    sitekey: str,
    step: Any = None,
    *,
    user_agent: str = '',
    locale: str = '',
    client: Any = None,
) -> dict[str, str]:
    """生成 Stripe payment_pages confirm 需要的 passive_captcha_token/ekey。

    历史 HAR 的浏览器 confirm 包含 passive_captcha_token，但这些样本
    不能证明缺少该字段就是 setup_attempt_failed / generic_decline 的原因。
    令牌无法离线伪造，必须使用当前 challenge 的 hsw.js 完成 PoW 并向
    api.hcaptcha.com/getcaptcha 换取。默认直接走真实 Chrome 求解；纯协议
    链(jsdom 跑 hsw.js)在 Stripe 上稳定返回 soft_reject，属于必然失败且
    每次额外下载约 1MB 的 hsw.js，因此仅在显式开启 UPI_HCAPTCHA_PROTOCOL=1
    时才会尝试。两条路径都失败时按可选字段降级（返回空、不阻塞建单），
    与 attestation/sentinel 的容错语义一致。
    """
    def log(status: str, detail: str) -> None:
        if callable(step):
            step('提交Stripe支付确认', status, detail)

    if str(os.environ.get('UPI_HCAPTCHA_DISABLE') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        log('warn', 'UPI_HCAPTCHA_DISABLE=1，已跳过 Stripe hCaptcha 被动验证')
        return {}
    if not sitekey:
        log('warn', 'Elements 未下发 passive_captcha.site_key，跳过 hCaptcha 被动验证（confirm 将缺失 passive_captcha_token）')
        return {}

    # 默认直接走真实 Chrome；纯协议链在 Stripe 上稳定 soft_reject，仅在显式
    # 开启 UPI_HCAPTCHA_PROTOCOL=1 时才尝试（省掉每账号每尝试约 1MB 的 hsw.js
    # 下载与注定失败的 jsdom PoW）。
    protocol_enabled = str(os.environ.get('UPI_HCAPTCHA_PROTOCOL') or '').strip().lower() in {
        '1', 'true', 'yes', 'on',
    }
    if protocol_enabled:
        try:
            protocol = _load_upi_hcaptcha_protocol_module()
            host = str(os.environ.get('UPI_HCAPTCHA_HOST') or 'js.stripe.com').strip()
            result = protocol.mint_hcaptcha_passive_protocol(
                site_key=sitekey,
                host=host,
                proxy=proxy,
                user_agent=str(os.environ.get('UPI_HCAPTCHA_USER_AGENT') or user_agent or '').strip(),
                hl=str(os.environ.get('UPI_HCAPTCHA_LOCALE') or locale or 'en').strip() or 'en',
                timeout=int(os.environ.get('UPI_HCAPTCHA_PROTOCOL_TIMEOUT') or 90),
                impersonate=str(
                    os.environ.get('UPI_HCAPTCHA_IMPERSONATE')
                    or (getattr(client, 'impersonate', '') if client is not None else '')
                    or ''
                ).strip(),
                on_stage=lambda name, status, detail: log(
                    status,
                    f'passive/{name}: {detail}',
                ),
            )
        except Exception as exc:
            result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        if isinstance(result, dict) and result.get('ok') and result.get('token'):
            out = {'passive_captcha_token': str(result['token'])}
            ekey = str(result.get('ekey') or '').strip()
            if ekey:
                out['passive_captcha_ekey'] = ekey
            log('ok', f'已通过 passive 协议生成 Stripe hCaptcha PoW 令牌（长度={len(out["passive_captcha_token"])}，n={result.get("n_len", 0)}，耗时={result.get("elapsed_ms", 0)}ms）')
            return out
        err = str((result or {}).get('error') or 'no_token')[:120] if isinstance(result, dict) else 'no_result'
        log('warn', f'passive 协议求解失败（{err}）')

    # 保留原有真实 Chrome 路径作为可选兜底；关闭协议或需要浏览器行为时
    # 可设置 UPI_HCAPTCHA_CHROME_FALLBACK=0。
    chrome_fallback = str(os.environ.get('UPI_HCAPTCHA_CHROME_FALLBACK') or '1').strip().lower() not in {
        '0', 'false', 'no', 'off',
    }
    if not chrome_fallback:
        log('warn', 'UPI_HCAPTCHA_CHROME_FALLBACK=0，未启用 Chrome 兜底')
        return {}
    if _upi_protocol_client(client):
        # 源站 upi-git5 从不求解 passive captcha；它的"无浏览器内核"正是相对于
        # 这一步而言。继续走 Chrome 求解会违背该 rail 的定义，所以协议模式下
        # 直接按可选字段降级（与 attestation/sentinel 的容错语义一致）。
        log('warn', '纯协议通道（upi-git5）不求解 passive captcha，confirm 将缺失 passive_captcha_token')
        return {}
    try:
        # The browser path can take several seconds while Chrome loads the
        # Stripe asset and executes its own fingerprint-bound PoW. Emit a
        # progress event so a protocol soft-reject is not mistaken for a
        # terminal failure in the UI.
        log('info', '开始 Chrome hCaptcha 被动求解')
        module = _load_upi_hcaptcha_module()
        # Align the solver UA with the client that submits confirm. hCaptcha
        # binds passive tokens to the observed browser fingerprint; the old
        # hard-coded Chrome/146 UA could produce a token that Stripe rejects.
        result = module.mint_hcaptcha_chrome(
            proxy=proxy,
            sitekey=sitekey,
            timeout=90,
            user_agent=str(user_agent or '').strip(),
            cookies=_client_cookies_map(client),
            # 未下载 Playwright 浏览器时，可用环境变量指向已安装的 Chrome/Edge/Chromium
            executable_path=str(
                os.environ.get('UPI_HCAPTCHA_CHROME_EXECUTABLE')
                or os.environ.get('UPI_CHROMIUM_EXECUTABLE')
                or ''
            ).strip(),
        )
    except Exception as exc:
        log('warn', f'hCaptcha 求解器调用异常：{str(exc)[:120]}；confirm 将缺失 passive_captcha_token')
        return {}
    if not (isinstance(result, dict) and result.get('ok') and result.get('token')):
        err = str((result or {}).get('error') or 'no_token')[:160] if isinstance(result, dict) else 'no_result'
        low = err.lower()
        hint = ''
        if 'playwright' in low or "executable doesn't exist" in low or 'chromium 启动失败' in err:
            hint = '（在引擎 Python 环境执行：pip install playwright && python -m playwright install chromium）'
        log('warn', f'hCaptcha 被动令牌求解失败（{err}）{hint}；confirm 将缺失 passive_captcha_token')
        return {}
    out = {'passive_captcha_token': str(result['token'])}
    ekey = str(result.get('ekey') or '').strip()
    if ekey:
        out['passive_captcha_ekey'] = ekey
    log('ok', f'已生成 Stripe hCaptcha 被动令牌（长度={len(out["passive_captcha_token"])}，ekey={"有" if ekey else "无"}）')
    return out


def _first_string(data: Any, keys: list[str]) -> str:
    if not isinstance(data, dict):
        return ''
    for key in keys:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ''


def _nested_string(data: Any, keys: tuple[str, ...] | list[str], *, max_depth: int = 8) -> str:
    """Find a named non-empty string in nested checkout/Stripe payloads.

    The normal HAR checkout response puts these fields at the top level, but
    the same endpoint has returned a nested ``checkoutConfig`` object for
    some sessions.  Keep the existing strict helper for unrelated protocol
    responses and use this defensive walker only where the checkout
    prerequisites are read.
    """
    wanted = {str(key) for key in keys}

    def walk(value: Any, depth: int) -> str:
        if depth > max_depth:
            return ''
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key) in wanted and isinstance(item, str) and item.strip():
                    return item.strip()
            for item in value.values():
                found = walk(item, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item, depth + 1)
                if found:
                    return found
        return ''

    return walk(data, 0)


def _named_string_from_text(text: str, keys: tuple[str, ...] | list[str]) -> str:
    """Extract a named string from JSON or React Router ``.data`` text.

    The checkout page data captured by the HAR is a devalue-style array,
    e.g. ``publishable_key","pk_live_..."`` rather than a JSON object.
    """
    source = str(text or '')
    for key in keys:
        escaped = re.escape(str(key))
        patterns = (
            rf'[\"\\]?{escaped}[\"\\]?\s*[:,]\s*[\"]([^\"]+)[\"]',
            rf'[\"]{escaped}[\"]\s*[,]\s*[\"]([^\"]+)[\"]',
        )
        for pattern in patterns:
            match = re.search(pattern, source, flags=re.I)
            if match and match.group(1).strip():
                return match.group(1).strip()
    return ''


def _checkout_prerequisite_string(payload: dict[str, Any], raw_text: str, keys: tuple[str, ...] | list[str]) -> str:
    return _nested_string(payload, keys) or _named_string_from_text(raw_text, keys)

def _expected_amount(payload: dict) -> int:
    due = payload.get('total_summary', {}).get('due') if isinstance(payload.get('total_summary'), dict) else None
    if due is not None:
        try:
            return int(due)
        except (TypeError, ValueError):
            pass
    invoice = payload.get('invoice', {})
    if isinstance(invoice, dict) and invoice.get('amount_due') is not None:
        try:
            return int(invoice['amount_due'])
        except (TypeError, ValueError):
            pass
    total = 0
    found = False
    for item in payload.get('line_items') or []:
        if isinstance(item, dict) and item.get('amount') is not None:
            try:
                total += int(item['amount'])
                found = True
            except (TypeError, ValueError):
                pass
    return total if found else 0

def _payment_method_types(payload: dict) -> list[str]:
    methods: list[str] = []

    def push(val: Any) -> None:
        if isinstance(val, str):
            m = val.strip().lower()
            if m and m not in methods:
                methods.append(m)
        elif isinstance(val, list):
            for item in val:
                push(item)
        elif isinstance(val, dict):
            if isinstance(val.get('type'), str):
                push(val['type'])
            # Stripe Custom Checkout's elements/sessions response exposes the
            # available methods under these names instead of the legacy
            # payment_method_types field.
            for key in ('ordered_payment_method_types_and_wallets', 'ordered_payment_method_types'):
                push(val.get(key))

    push(payload.get('payment_method_types'))
    push(payload.get('ordered_payment_method_types_and_wallets'))
    preference = payload.get('payment_method_preference')
    if isinstance(preference, dict):
        push(preference.get('ordered_payment_method_types'))
    specs = payload.get('payment_method_specs')
    if isinstance(specs, list):
        for spec in specs:
            if isinstance(spec, dict):
                push(spec.get('type'))
    return methods

def _ensure_upi_offered(payload: dict, phase: str, *, force: bool = False, on_warn: Any = None) -> list[str]:
    methods = _payment_method_types(payload)
    if 'upi' not in methods:
        if force:
            # 对齐 MoMo 的 --force-payment-method：payment_method_types 不含 upi
            # 时仍继续提交，不在这里本地中断，最终仍由 Node 边界校验 0 元链接。
            if callable(on_warn):
                on_warn(phase, methods)
            return methods
        raise RuntimeError(f"{phase} checkout does not offer upi (India-only); methods={','.join(methods) or 'none'}; need Indian egress proxy (region-IN) for checkout/provider stages")
    return methods


def _upi_promo_campaign() -> dict[str, Any]:
    """Return the campaign object used by the real checkout create request.

    The supplied HAR is an original-price checkout and therefore has
    ``promo_campaign: null`` in the response.  The checkout web client uses
    this object on the *initial* ``payments/checkout`` payload when a campaign
    is active; the HAR does not contain a separate post-create session-update request.
    Keep the field construction in one place so the UPI request cannot drift
    back to the old, unsupported update flow.
    """
    return {
        'promo_campaign_id': UPI_PROMO_COUPON,
        'is_coupon_from_query_param': False,
    }


def _upi_promo_marker(payload: Any) -> str:
    """Best-effort status for diagnostics; final truth is the taxes total."""
    if not isinstance(payload, dict):
        return 'unknown'
    campaign = payload.get('promo_campaign')
    if isinstance(campaign, dict):
        campaign_id = str(campaign.get('promo_campaign_id') or '').strip()
        if campaign_id == UPI_PROMO_COUPON:
            return 'applied'
    elif str(campaign or '').strip() == UPI_PROMO_COUPON:
        return 'applied'

    def positive_discount(value: Any) -> bool:
        if isinstance(value, list):
            return any(positive_discount(item) for item in value)
        if isinstance(value, dict):
            for key in ('minorUnitsAmount', 'amount', 'amount_discount', 'discount'):
                if key in value:
                    try:
                        if int(value.get(key) or 0) > 0:
                            return True
                    except (TypeError, ValueError):
                        pass
            return any(positive_discount(item) for item in value.values())
        try:
            return int(value or 0) > 0
        except (TypeError, ValueError):
            return False

    checkout_state = payload.get('checkout_state') if isinstance(payload.get('checkout_state'), dict) else {}
    if positive_discount(checkout_state.get('discountAmounts')):
        return 'applied'
    total = checkout_state.get('total') if isinstance(checkout_state.get('total'), dict) else {}
    if positive_discount(total.get('discount')):
        return 'applied'
    # Some responses expose the post-campaign amount without the campaign
    # object.  A zero total is still an explicit successful application.
    amount = _amount_from_payload(payload)
    return 'applied' if amount == 0 else 'not_applied' if campaign is None else 'unknown'


def _upi_promo_error(detail: str) -> bool:
    text = str(detail or '').lower()
    if any(mark in text for mark in (
        'this promotion is not available',
        'promotion is not available',
        'invalid promotion',
        'invalid_promotion',
        'promo not eligible',
        'promotion not eligible',
        'coupon not eligible',
        'coupon is not eligible',
        'already redeemed',
        'promotion redeemed',
        'coupon redeemed',
    )):
        return True
    # Keep generic 403/risk-control responses out of the eligibility bucket.
    return bool(
        re.search(r'(?:promotion|promo[_ -]?campaign|coupon|discount).{0,96}(?:ineligible|not eligible|unavailable|not available|invalid|expired|redeemed|rejected)', text)
        or re.search(r'(?:ineligible|not eligible|unavailable|not available|invalid|expired|redeemed|rejected).{0,96}(?:promotion|promo[_ -]?campaign|coupon|discount)', text)
    )


def _is_upi_risk_blocked_response(status: int, text: str) -> bool:
    """源站的风控拦截判定（app.py:1213-1217）。

    只有三个信号算 blocked：HTTP 403、正文里的 unusual_activity、正文里的
    '"status":"blocked"'。

    刻意不做递归 payload 扫描：confirm/approve 的正常失败响应里会嵌套
    submission_attempt / payment_intent 这类对象，凡是递归找 status/result/
    state 字段都会被它们命中，把 generic_decline（支付方式设置失败）误判成
    风控拦截。源站从不做这种匹配，因此最坏也只应该失败在"支付通道拒绝"。
    """
    try:
        if int(status or 0) == 403:
            return True
    except (TypeError, ValueError):
        pass
    lowered = str(text or '').lower()
    if 'unusual_activity' in lowered or 'unusual activity' in lowered:
        return True
    return '"status":"blocked"' in lowered.replace(' ', '')


def _raise_if_upi_risk_blocked(status: int, text: str, label: str, payload: Any = None) -> None:
    if _is_upi_risk_blocked_response(status, text):
        raise RuntimeError(f'UPI risk_blocked: {label} HTTP {status}; {str(text or "")[:240]}')


def _json_or_error(status: int, text: str, label: str) -> dict:
    _raise_if_upi_risk_blocked(status, text, label)
    if status >= 400:
        preview = (text or '')[:240]
        raise RuntimeError(localize_upi_error(f'{label} failed: HTTP {status} {preview}'))
    try:
        data = json.loads(text or '{}')
    except json.JSONDecodeError as exc:
        raise RuntimeError(localize_upi_error(f'{label} invalid JSON')) from exc
    if not isinstance(data, dict):
        raise RuntimeError(localize_upi_error(f'{label} unexpected response'))
    _raise_if_upi_risk_blocked(status, text, label, data)
    return data


def _locked_stripe_request_ids(client: Any) -> dict[str, str]:
    values = getattr(client, 'stripe_request_ids', {})
    values = values if isinstance(values, dict) else {}
    locked = {
        key: str(values.get(key) or '').strip()
        for key in ('guid', 'muid', 'sid', 'stripe_js_id')
    }
    missing = [key for key, value in locked.items() if not value]
    if missing:
        raise RuntimeError(
            'UPI 风控上下文生成失败：当前 Firefox 支付页未捕获 Stripe '
            + ','.join(missing)
        )
    return locked


def _is_setup_intent_failure(error: Any) -> bool:
    """Whether an exception is the Stripe SetupIntent/payment-method failure.

    This deliberately matches only the payment-method creation failure family;
    auth, promotion, checkout and network errors must not be retried here.
    """
    text = str(error or '').lower()
    if 'upi custom checkout submission failed' in text:
        return bool(re.search(r'setup_error=(?!-)(?:[a-z0-9_])', text))
    return any(marker in text for marker in (
        'setup_attempt_failed',
        'generic_decline',
        'checkout_approval_payment_failure_with_payment_error',
        'upi_payment_setup_declined',
    ))


def _setup_intent_proxy_candidates(current: str, pool: list[str] | None) -> list[str]:
    """Return current proxy followed by distinct pool entries.

    Proxy strings are normalized only for this stage. The client/session and
    fingerprint remain untouched; callers select a different transport for the
    next SetupIntent submission.
    """
    values: list[str] = []
    for value in [current, *(pool or [])]:
        normalized = _normalize_upi_proxy_url(str(value or '').strip())
        if normalized and normalized not in values:
            values.append(normalized)
    return values or [str(current or '').strip()]

def _is_upi_url(url: str) -> bool:
    u = str(url or '').strip()
    if u.lower().startswith('upi://'):
        return True
    try:
        parsed = urlparse(u)
        host = (parsed.hostname or '').lower()
        path = parsed.path or ''
        if host == 'hooks.stripe.com' and path.startswith('/redirect/'):
            return True
        if 'stripe.com' in host and ('upi' in path or path.startswith('/redirect/')):
            return True
    except Exception:
        return False
    return False

def _extract_upi_from_text(text: str) -> str:
    for regex in (UPI_SCHEME_RE, STRIPE_UPI_INSTRUCTIONS_RE, STRIPE_HOSTED_RE, PM_REDIRECTS_RE):
        m = regex.search(text or '')
        if m and _is_upi_url(m.group(0)):
            return m.group(0)
    return ''

def _payment_page_poll_summary(payload: Any) -> str:
    """Return a small, credential-free diagnostic for a failed payment-page poll."""
    if not isinstance(payload, dict):
        return 'invalid payment-page response'

    def code(value: Any) -> str:
        return value if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value) else '-'

    keys = ','.join(code(key) for key in list(payload.keys())[:8])
    submission = payload.get('submission_attempt')
    submission = submission if isinstance(submission, dict) else {}
    submission_error = submission.get('error')
    submission_error = submission_error if isinstance(submission_error, dict) else {}
    payment_error = submission_error.get('payment_error')
    payment_error = payment_error if isinstance(payment_error, dict) else {}

    setup_intent = payload.get('setup_intent')
    setup_intent = setup_intent if isinstance(setup_intent, dict) else {}
    setup_error = setup_intent.get('last_setup_error')
    setup_error = setup_error if isinstance(setup_error, dict) else {}

    state = submission.get('state') or payload.get('state')
    status = payload.get('status') or payload.get('payment_status') or setup_intent.get('status')
    error_code = submission_error.get('code')
    payment_error_code = payment_error.get('code')
    setup_error_code = setup_error.get('code')
    decline_code = payment_error.get('decline_code') or setup_error.get('decline_code')
    payment_intent = payload.get('payment_intent')
    payment_intent_present = bool(payment_intent)
    payment_intent = payment_intent if isinstance(payment_intent, dict) else {}
    payment_intent_status = payment_intent.get('status')
    intent_error = payment_intent.get('last_payment_error')
    intent_error = intent_error if isinstance(intent_error, dict) else {}
    setup_intent_present = bool(setup_intent)
    method_type = '-'
    for holder in (setup_intent, payment_intent, setup_error, intent_error, payment_error):
        method = holder.get('payment_method')
        if isinstance(method, dict) and code(method.get('type')) != '-':
            method_type = code(method.get('type'))
            break
    return (
        # The adapter retains 500 characters per stage. Put the actionable
        # states and error codes before the optional shape diagnostics.
        f"state={code(state)}; status={code(status)}; "
        f"setup_status={code(setup_intent.get('status'))}; payment_status={code(payment_intent_status)}; "
        f"payment_method_type={method_type}; "
        f"error={code(error_code)}; payment_error={code(payment_error_code)}; "
        f"setup_error={code(setup_error_code)}; decline={code(decline_code)}; "
        f"intent_error={code(intent_error.get('code'))}; intent_decline={code(intent_error.get('decline_code'))}; "
        f"payment_intent={'present' if payment_intent_present else 'missing'}({code(payment_intent_status)})"
        f" setup_intent={'present' if setup_intent_present else 'missing'}; "
        f"keys=[{keys}]"
    )


def _upi_http_diagnostic(status: int, headers: Any) -> str:
    """Keep only the HTTP status and an upstream request identifier."""
    request_id = '-'
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() in {'request-id', 'x-request-id'}:
                if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value):
                    request_id = value
                    break
    return f'HTTP={int(status)}; request_id={request_id}'


def _raise_for_upi_payment_page_failure(payload: dict[str, Any]) -> None:
    submission = payload.get('submission_attempt')
    if isinstance(submission, dict) and str(submission.get('state') or '').strip().lower() == 'failed':
        raise RuntimeError('UPI Custom Checkout submission failed: ' + _payment_page_poll_summary(payload))
    for key in ('setup_intent', 'payment_intent'):
        intent = payload.get(key)
        if not isinstance(intent, dict):
            continue
        state = str(intent.get('status') or '').strip().lower()
        # A newly exposed intent can still be waiting for its payment method.
        # Only reject that page state when the provider also reports an error.
        if state == 'canceled' or (state == 'requires_payment_method' and (
            intent.get('last_setup_error') or intent.get('last_payment_error')
        )):
            _raise_for_upi_intent_failure(intent)

def _find_upi_action_url(value: Any) -> str:
    if isinstance(value, dict):
        na = value.get('next_action') or {}
        if isinstance(na, dict):
            upi = na.get('upi_handle_redirect_or_display_qr_code') or {}
            if isinstance(upi, dict):
                hosted = str(upi.get('hosted_instructions_url') or '').strip()
                if hosted:
                    return hosted
                qr = upi.get('qr_code') or {}
                if isinstance(qr, dict):
                    # 深链优先；否则退回二维码图片 URL（qr.stripe.com），
                    # 不再死等 hosted_instructions_url 字段。
                    for k in ('data', 'url', 'image_url_svg', 'image_url_png', 'image_url'):
                        v = str(qr.get(k) or '').strip()
                        if v and (v.lower().startswith('upi://') or v.lower().startswith('http')):
                            return v
            redirect = na.get('redirect_to_url') or {}
            if isinstance(redirect, dict):
                url = str(redirect.get('url') or '').strip()
                if url:
                    return url
        for key in ('url', 'redirect_url', 'hosted_url'):
            url = str(value.get(key) or '').strip()
            if url and url.lower().startswith(('http', 'upi://')):
                return url
        for item in value.values():
            found = _find_upi_action_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_upi_action_url(item)
            if found:
                return found
    elif isinstance(value, str):
        return _extract_upi_from_text(value)
    return ''

def _extract_redirect_url(payload: Any) -> str:
    if isinstance(payload, dict):
        found = _find_upi_action_url(payload)
        if found:
            return found
        na = payload.get('next_action') or {}
        if isinstance(na, dict):
            redirect = na.get('redirect_to_url') or {}
            if isinstance(redirect, dict):
                url = str(redirect.get('url') or '').strip()
                if url:
                    return url
        for item in payload.values():
            found = _extract_redirect_url(item)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _extract_redirect_url(item)
            if found:
                return found
    elif isinstance(payload, str):
        return _extract_upi_from_text(payload)
    return ''

def _confirm_requires_approval(payload: dict) -> bool:
    return str((payload.get('submission_attempt') or {}).get('state') or '') == 'requires_approval'

def _chatgpt_success_return_url(cs_id: str, processor_entity: str) -> str:
    return f'https://chatgpt.com/checkout/success?processor_entity={processor_entity}&checkout_session_id={cs_id}'

def _stripe_confirm_return_url(cs_id: str, processor_entity: str, stripe_hosted_url: str) -> str:
    hosted = stripe_hosted_url.strip() or f'https://checkout.stripe.com/c/pay/{cs_id}'
    parsed = urlparse(hosted)
    if parsed.hostname not in ('pay.openai.com', 'checkout.stripe.com'):
        return hosted
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if 'success_return_url' not in query:
        query['success_return_url'] = _chatgpt_success_return_url(cs_id, processor_entity)
    new_query = urlencode(query)
    return parsed._replace(query=new_query).geturl()

def _upi_supplied_address(billing: dict[str, Any] | None) -> dict[str, str] | None:
    """Validate an explicitly entered address, leaving absent addresses optional."""
    if billing is not None and not isinstance(billing, dict):
        raise ValueError('UPI 账单地址格式错误：billing 必须是 JSON 对象')
    source = billing if isinstance(billing, dict) else {}
    if source.get('address') is not None and not isinstance(source['address'], dict):
        raise ValueError('UPI 账单地址格式错误：address 必须是 JSON 对象')
    address = source.get('address') if isinstance(source.get('address'), dict) else {}

    def value(key: str, *aliases: str) -> str:
        for item in (key, *aliases):
            candidate = address.get(item) if item in address else source.get(item)
            if candidate is None:
                continue
            if not isinstance(candidate, str) and not (
                key == 'postal_code' and isinstance(candidate, int) and not isinstance(candidate, bool)
            ):
                raise ValueError(f'UPI 账单地址格式错误：{key} 必须是文本字段')
            if str(candidate).strip():
                return str(candidate).strip()
        return ''

    country = value('country') or 'IN'
    if country.casefold() not in {'in', 'india'}:
        raise ValueError('UPI 账单地址国家必须为 IN（印度），请修正填写的国家')
    fields = {key: value(key) for key in ('line1', 'line2', 'city', 'state')}
    fields['postal_code'] = value('postal_code', 'postal')
    if not any(fields.values()):
        return None
    if not all(fields[key] for key in ('line1', 'city', 'state', 'postal_code')):
        raise ValueError('UPI 账单地址不完整：请填写街道、城市、邦和六位邮编')
    if not _is_complete_indian_address(fields['line1'], fields['city'], fields['state'], fields['postal_code'], country):
        raise ValueError('UPI 账单地址未通过本地校验：请核对国家、城市、邦和邮编；本地地址池可能尚未覆盖该城市或邮区')
    fields['state'] = _upi_state_code(fields['state'])
    city_key = _upi_city_name(fields['city']).casefold()
    fields['city'] = next(row[1] for row in _upi_billing_address_rows() if row[1].casefold() == city_key)
    fields['country'] = 'IN'
    return fields


def _normalize_upi_billing(
    billing: dict[str, Any] | None,
    email: str,
    *,
    preferred_city: str = '',
) -> dict[str, str]:
    """Keep valid caller addresses; use the pool only when no address was entered."""
    source = billing if isinstance(billing, dict) else {}
    address = _upi_supplied_address(billing)
    if address is None:
        selected = _random_upi_billing_address(preferred_city=preferred_city)
        address = selected['address']
        default_name = selected['name']
    else:
        default_name = ''
    name = str(source.get('name') or '').strip()
    if not name:
        name = default_name or random.SystemRandom().choice(UPI_BILLING_NAMES)
    return {
        'name': name,
        'email': str(source.get('email') or '').strip() or str(email or '').strip() or f'checkout-{uuid.uuid4().hex[:10]}@outlook.com',
        'line1': address['line1'],
        'line2': address.get('line2', ''),
        'city': address['city'],
        'state': INDIA_STATE_NAMES[address['state']],
        'postal_code': address['postal_code'],
        'country': 'IN',
    }


def build_upi_billing(
    email: str = '',
    billing: dict[str, Any] | None = None,
    *,
    preferred_city: str = '',
) -> dict[str, str]:
    """Normalize one attempt's billing data for reuse throughout Checkout."""
    return _normalize_upi_billing(billing, email, preferred_city=preferred_city)


def _ensure_upi_not_cancelled(cancelled: Any) -> None:
    if callable(cancelled) and cancelled():
        raise InterruptedError('任务已停止')


def _upi_status_cn(value: Any) -> str:
    raw = str(value or '').strip().lower()
    return {
        'applied': '已生效',
        'not_applied': '未生效',
        'not_requested': '未提交',
        'eligible': '有资格',
        'not_eligible': '无资格',
        'unknown': '未知',
        'custom': '自定义支付页',
        'deferred': '延迟支付页',
        'requires_action': '等待扫码',
        'requires_confirmation': '等待确认',
        'processing': '处理中',
        'succeeded': '支付成功',
        'failed': '失败',
        'blocked': '风控拦截',
    }.get(raw, raw or '未知')

def extract_upi_link(access_token: str, proxy: str | None=None, *, proxy_pool: list[str] | None = None, promotion_proxy: str | None=None, billing: dict[str, Any] | None=None, fingerprint: dict[str, Any] | None=None, job_id: str='', on_step: Any=None, on_qr_update: Any=None, cancelled: Any=None, use_promo: bool = True, promo_campaign: str | None = None, checkout_ui_mode: str = 'auto', force_payment_method: bool = False, force_zero: bool = False, wait_paid: bool = False, paid_timeout: float = 900.0, upi_rail: str = 'upi', upi_setup_v2: bool = False, require_server_upi_mandate: bool = False, requested_upi_mandate: dict[str, Any] | None = None, upi_har_context: bool = False) -> dict[str, Any]:
    if not use_promo:
        raise RuntimeError('UPI only supports zero-due promotional checkout; original-price links are disabled')
    # Reject bad user input before authentication, proxy probes or Checkout.
    supplied_address = _upi_supplied_address(billing)
    proxy_url = _normalize_upi_proxy_url(str(proxy or resolve_upi_proxy() or '').strip())
    if not proxy_url:
        raise RuntimeError('proxy is required for UPI extraction')
    # Normalize once at the attempt boundary. The legacy UPI chain uses the
    # payment proxy for Checkout/Stripe and the separately supplied promotion
    # proxy for coupon eligibility. When no promotion proxy is supplied both
    # stages intentionally collapse to the payment proxy.
    setup_proxy_pool = [proxy_url]
    campaign_id = str(promo_campaign or UPI_PROMO_COUPON).strip()
    preferred_city = ''
    if supplied_address is None and _upi_billing_sync_city():
        if proxy_url not in _EXIT_CITY_BY_PROXY:
            try:
                _EXIT_CITY_BY_PROXY[proxy_url] = _probe_upi_exit_city(proxy_url)
            except Exception:
                _EXIT_CITY_BY_PROXY[proxy_url] = ''
        preferred_city = _EXIT_CITY_BY_PROXY.get(proxy_url, '')
    promotion_proxy_url = _normalize_upi_proxy_url(
        str(promotion_proxy or proxy_url).strip()
    ) or proxy_url
    steps: list[dict[str, str]] = []
    _stage_last_status: dict[str, str] = {}

    # Keep the authentication/proxy stages visible even when the network
    # fails before Checkout is created. Previously these steps were declared
    # only after token resolution, so a proxy failure was reported as the
    # generic ``start`` stage by the distributed worker.
    def step(name: str, status: str, detail: str) -> None:
        status = str(status or 'info').strip().lower()
        terminal = status in ('ok', 'warn', 'fail', 'err', 'error')
        if terminal and name not in _stage_last_status:
            steps.append({'name': name, 'status': 'run', 'detail': '开始'})
            _stage_last_status[name] = 'run'
            if callable(on_step):
                try:
                    on_step(list(steps))
                except Exception:
                    pass
        _stage_last_status[name] = status
        steps.append({'name': name, 'status': status, 'detail': detail})
        if callable(on_step):
            try:
                on_step(list(steps))
            except Exception:
                pass

    step('代理准备', 'ok', f'已准备 UPI 支付代理和优惠代理')
    raw_session = str(access_token or '').strip()
    orig_token = normalize_access_token(raw_session)
    last_error: Exception | None = None
    for attempt in range(1, MAX_REBUILD_ATTEMPTS + 1):
        if attempt > 1:
            step('重新建单', 'info', f'本次尝试 {attempt}/{MAX_REBUILD_ATTEMPTS}')
        _ensure_upi_not_cancelled(cancelled)
        client = UpiHttpClient(proxy_url, region='IN', fingerprint=fingerprint, upi_rail=upi_rail)
        watch_started = False
        try:
            # Authentication, promotion eligibility, Checkout and Stripe all
            # share this exact client. This keeps the proxy, Cookie jar,
            # device/session ids and browser fingerprint stable for one
            # attempt; the distributed worker creates a fresh profile on the
            # next outer retry.
            step('登录会话刷新', 'run', '正在校验账号登录会话')
            token = resolve_upi_access_token(
                raw_session,
                proxy_url,
                fingerprint=fingerprint,
                client=client,
            )
            if not token:
                raise ValueError('Access Token is required')
            email = _jwt_email(token)
            step('登录会话刷新', 'ok', '账号登录会话已校验')
            if token != orig_token:
                step('登录会话刷新', 'ok', '已通过会话凭证刷新账号登录凭证')

            # 优惠资格预检只作诊断，绝不作终态判定。
            #
            # 源站从不调用 check_coupon，它的入口预检是 payments/payment_methods，
            # 并且明确注释「one_click_trial_eligible 仅为 payment_methods 字段，
            # 不作为活动资格判定」（app.py:1332-1333）。真正的判定交给建单后的
            # 「实际应付金额是否为 0」。
            #
            # 这里曾把 not_eligible / already_redeemed 直接抛成终态，导致账号在
            # 真实建单之前就被判死：同一账号同一条代理，源站能走到 Checkout 阶段
            # 才报「优惠未归零：amount=189736」，移植版却停在更早的预检上，报出
            # 一个源站不会产生的错误。
            promo_state = 'unknown'
            if use_promo:
                try:
                    promo_state = _fetch_promo_coupon_state(
                        token,
                        proxy_url,
                        promotion_proxy_url,
                        fingerprint=fingerprint,
                        client=client,
                        promo_campaign=campaign_id,
                    )
                except Exception:
                    promo_state = 'unknown'
                promo_label = {
                    'eligible': '有资格',
                    'not_eligible': '无资格',
                    'already_redeemed': '已领取',
                    'unknown': '暂未确认',
                }.get(promo_state, promo_state)
                step(
                    '优惠资格预检',
                    'info' if promo_state in ('eligible', 'unknown') else 'warn',
                    f'优惠资格预检={promo_label}（仅供参考，不中止任务）；'
                    '建单时将提交 0 元优惠活动，最终以实际应付金额是否为 0 判定',
                )
            else:
                step('优惠资格预检', 'info', '未提交优惠活动')

            # Each attempt reuses caller billing or selects one pool address;
            # taxes, snapshot and confirmation share this same result.
            upi_billing = build_upi_billing(
                email,
                billing,
                preferred_city=preferred_city,
            )
            step(
                '账单地址',
                'info',
                f'本次账单地址：{upi_billing["city"]}/{upi_billing["state"]} {upi_billing["postal_code"]}',
            )
            result = _run_upi_attempt(client, token, step, steps, promotion_proxy=promotion_proxy_url, billing=upi_billing, setup_proxies=setup_proxy_pool, use_promo=use_promo, promo_campaign=campaign_id, on_qr_update=on_qr_update, cancelled=cancelled, checkout_ui_mode=checkout_ui_mode, force_payment_method=force_payment_method, force_zero=force_zero, wait_paid=wait_paid, paid_timeout=paid_timeout, upi_rail=upi_rail, upi_setup_v2=upi_setup_v2, require_server_upi_mandate=require_server_upi_mandate, requested_upi_mandate=requested_upi_mandate, upi_har_context=upi_har_context)
            watch_started = bool(result.pop('_watch_started', False))
            result['steps'] = steps
            return result
        except Exception as exc:
            last_error = exc
            detail = str(exc)
            if 'blocked by risk control' in detail.lower():
                raise
            if not _is_rebuild_retryable(exc) or attempt >= MAX_REBUILD_ATTEMPTS:
                raise
            step('重新建单重试', 'warn', localize_upi_error(detail))
            _human_pause('long')
        finally:
            if not watch_started:
                try:
                    client.close()
                except Exception:
                    pass
    raise last_error or RuntimeError('extract failed after rebuild retries')

def _minor_units_amount(value: Any) -> int | None:
    """Parse an integer minor-unit amount without rounding an upstream value."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and value != amount:
        return None
    return amount


def _amount_from_payload(payload: Any) -> int | None:
    if not isinstance(payload, (dict, list)):
        return None

    scalar = _minor_units_amount

    def walk(value: Any) -> tuple[bool, int | None]:
        # An explicitly returned but invalid total must stay unknown. Keep
        # presence separate from its parsed value so a zero line item or
        # discount cannot replace a malformed authoritative amount.
        if isinstance(value, dict):
            # Explicit API totals always win over nested line-item amounts.
            for key in ('amount_total', 'amount_due', 'due'):
                if key in value:
                    return True, scalar(value[key])

            # Checkout payloads use checkout_state.total.total.minorUnitsAmount.
            # Prefer the innermost total before subtotal/discount children;
            # otherwise a discounted checkout can be mistaken for its subtotal.
            total = value.get('total')
            if isinstance(total, dict):
                nested_total = total.get('total')
                if isinstance(nested_total, dict) and 'minorUnitsAmount' in nested_total:
                    return True, scalar(nested_total['minorUnitsAmount'])
                if 'minorUnitsAmount' in total:
                    return True, scalar(total['minorUnitsAmount'])
                present, found = walk(total)
                if present:
                    return True, found

            for key in ('total_summary', 'invoice', 'checkout_state', 'checkout_session'):
                if key in value:
                    present, found = walk(value.get(key))
                    if present:
                        return True, found

            # Generic amount is retained as a last-resort fallback for Stripe
            # and legacy responses, but never before an explicit total.
            for key in ('amount',):
                if key in value:
                    return True, scalar(value[key])
            for child in value.values():
                if isinstance(child, (dict, list)):
                    present, found = walk(child)
                    if present:
                        return True, found
        elif isinstance(value, list):
            for child in value:
                present, found = walk(child)
                if present:
                    return True, found
        return False, None

    return walk(payload)[1]


def _zero_warn(step: Any, stage: str):
    """Return an on_warn callback for _require_zero_due (强压 0 元日志)。"""
    def _warn(source: str, amount) -> None:
        if callable(step):
            step(stage, 'warn', f'{source} 未确认服务端 0 元（amount={amount}），已按 --force-zero 继续 confirm/start（实际金额以服务端为准）')
    return _warn


def _require_zero_due(amount: int | None, source: str, *, force: bool = False, on_warn: Any = None) -> int:
    # 金额不是 0 只证明本次 Checkout 没有应用优惠，不足以证明账号没有资格。
    # 资格拒绝只有在上游明确返回 promotion/coupon eligibility 错误时才分类为
    # “账号无优惠资格”（见 _upi_promo_error）。
    verified_amount = _minor_units_amount(amount)
    if verified_amount is None:
        if force:
            if callable(on_warn):
                on_warn(source, amount)
            return 0
        raise RuntimeError(f'upi promo amount unavailable: source={source}')
    if verified_amount != 0:
        if force:
            # 强压 0 元（对齐 MoMo/GoPay --force-zero）：本地不再中断，继续
            # confirm/start；最终 0 元链接的校验仍由 Node 边界完成。
            if callable(on_warn):
                on_warn(source, verified_amount)
            return 0
        raise RuntimeError(f'upi promo not applied: source={source}; amount={verified_amount}')
    return verified_amount


def _is_checkout_session_id(value: str) -> bool:
    text = str(value or '').strip()
    return text.startswith(('cs_', 'oaics_'))


def _payment_intent_id(value: Any) -> str:
    text = str(value or '').strip()
    if '_secret_' in text:
        text = text.split('_secret_', 1)[0]
    if text.startswith('pi_'):
        return text
    return ''


def _checkout_intent(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Resolve the intent returned by Checkout without mixing IDs or secrets."""
    declared = str(payload.get('type') or '').strip()
    if declared and declared not in {'payment_intent', 'setup_intent'}:
        raise RuntimeError('UPI checkout/confirm unsupported intent type')
    secrets_found: set[str] = set()
    ids: set[str] = set()
    kinds: set[str] = {declared} if declared else set()
    for holder, obj in [('', payload), *[(key, payload.get(key)) for key in ('payment_intent', 'setup_intent')]]:
        if isinstance(obj, str):
            value = obj.strip()
            if value:
                ids.add(value)
                kinds.add(holder)
            continue
        if not isinstance(obj, dict):
            continue
        secret = _first_string(obj, ['client_secret'])
        intent_id = _first_string(obj, ['id'])
        if secret:
            secrets_found.add(secret)
        if intent_id:
            ids.add(intent_id)
        if holder and (secret or intent_id):
            kinds.add(holder)
    if len(secrets_found) != 1:
        raise RuntimeError('UPI checkout/confirm missing or conflicting intent client_secret')
    secret = next(iter(secrets_found))
    intent_id, separator, secret_value = secret.partition('_secret_')
    if not separator or not secret_value or not re.fullmatch(r'(?:pi|seti)_[A-Za-z0-9_]+', intent_id):
        raise RuntimeError('UPI checkout/confirm invalid intent client_secret')
    kind = 'setup_intent' if intent_id.startswith('seti_') else 'payment_intent'
    if (kinds and kinds != {kind}) or any(value != intent_id for value in ids):
        raise RuntimeError('UPI checkout/confirm conflicting intent type or id')
    return kind, intent_id, secret


def _har_return_url(cs_id: str, processor_entity: str) -> str:
    return f'https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={processor_entity}&plan_type=plus'


def _elements_session(
    client: UpiHttpClient,
    *,
    customer_session_secret: str = '',
    stripe_pk: str,
    amount: int,
    stripe_js_id: str,
    proxy: str,
    checkout_session_id: str = '',
    checkout_ui_mode: str = 'deferred',
    use_promo: bool = True,
    payment_method_configuration_id: str = '',
    session_id: str = '',
    payment_method_types: list[str] | None = None,
    stripe_version: str = '',
) -> dict[str, Any]:
    # The current UPI Checkout response is Stripe Custom Checkout.  It does
    # not return a CustomerSession secret; Stripe ties the Elements session to
    # the Checkout Session id instead.  Keep the old deferred-intent shape for
    # older accounts, but make the CustomerSession field conditional.
    custom = str(checkout_ui_mode or '').strip().lower() == 'custom'
    # OAICS uses the base Elements contract (HAR entries 568/657). The
    # payment_pages beta flags belong only to Stripe Custom Checkout.
    client_fp = getattr(client, 'fingerprint', {})
    client_fp = client_fp if isinstance(client_fp, dict) else {}
    client_region = str(getattr(client, 'region', '') or client_fp.get('country') or '').upper()
    if custom:
        # 自定义 Checkout 同样使用当前会话的语言/时区（HAR: hi-IN / Asia/Calcutta）
        elements_locale = _stripe_browser_locale(client)
        elements_timezone = _stripe_browser_timezone(client)
    else:
        elements_locale = str(getattr(client, 'locale', '') or client_fp.get('locale') or IN_BROWSER_LOCALE)
        elements_timezone = str(
            getattr(client, 'timezone', '') or client_fp.get('timezone')
            or ('Asia/Kolkata' if client_region == 'IN' else 'Asia/Ho_Chi_Minh')
        )
    methods: list[str] = []
    for value in payment_method_types or []:
        method = str(value or '').strip().lower()
        if method and method not in methods:
            methods.append(method)
    if not methods:
        if custom:
            methods = ['card', 'link', 'upi']
        else:
            raise RuntimeError('UPI OAICS Checkout payment method list is empty')
    params = {
        'deferred_intent[mode]': 'subscription',
        'deferred_intent[amount]': str(amount),
        'deferred_intent[currency]': 'inr',
        'deferred_intent[setup_future_usage]': 'off_session',
        'currency': 'inr',
        'key': stripe_pk,
        '_stripe_version': str(stripe_version or (STRIPE_CUSTOM_VERSION if custom else STRIPE_VERSION)),
        'elements_init_source': 'custom_checkout' if custom else 'stripe.elements',
        'referrer_host': 'chatgpt.com',
        'stripe_js_id': stripe_js_id,
        'locale': elements_locale,
        'type': 'deferred_intent',
    }
    # The reference negotiates the browser timezone only on the deferred-intent
    # (oaics_/Momo) contract; custom Checkout identifies the browser through
    # checkout_session_id instead. Sending it there breaks the HAR shape.
    if not custom:
        params['browser_timezone'] = elements_timezone
    for index, method in enumerate(methods):
        params[f'deferred_intent[payment_method_types][{index}]'] = method
    if custom:
        params.update({
            # These are retained explicitly for the supplied UPI HAR shape.
            'client_betas[0]': 'custom_checkout_server_updates_1',
            'client_betas[1]': 'custom_checkout_manual_approval_1',
        })
        if checkout_session_id:
            params['checkout_session_id'] = checkout_session_id
        if session_id:
            params['session_id'] = session_id
    if not use_promo and payment_method_configuration_id:
        params['deferred_intent[payment_method_configuration][id]'] = payment_method_configuration_id
    if not custom and customer_session_secret:
        params['customer_session_client_secret'] = customer_session_secret
    url = 'https://api.stripe.com/v1/elements/sessions?' + urlencode(params)
    headers = _stripe_headers(client)
    headers['content-type'] = 'application/x-www-form-urlencoded'
    status, text, _ = client.request('GET', url, headers=headers, proxy=proxy)
    return _json_or_error(status, text, 'stripe elements session')



STRIPE_CUSTOM_VERSION = '2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1'


def _custom_checkout_elements_options() -> dict[str, str]:
    return {
        'elements_options_client[saved_payment_method][enable_save]': 'auto',
        'elements_options_client[saved_payment_method][enable_redisplay]': 'auto',
    }


def _custom_checkout_client_fields(*, stripe_js_id: str, elements: dict[str, Any], init_data: dict[str, Any] | None = None, checkout_session_id: str = '', use_promo: bool = True, locale: str = '') -> dict[str, str]:
    init_data = init_data if isinstance(init_data, dict) else {}
    checkout_config_id = str(init_data.get('config_id') or '').strip()
    elements_session_id = str(elements.get('session_id') or '').strip()
    elements_config_id = str(elements.get('config_id') or '').strip()
    fields = {
        'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1',
        'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1',
        'elements_session_client[elements_init_source]': 'custom_checkout',
        'elements_session_client[referrer_host]': 'chatgpt.com',
        'elements_session_client[session_id]': elements_session_id,
        'elements_session_client[stripe_js_id]': stripe_js_id,
        'elements_session_client[locale]': str(locale or '').strip() or IN_BROWSER_LOCALE,
        'elements_session_client[is_aggregation_expected]': 'false',
        'client_attribution_metadata[client_session_id]': stripe_js_id,
        'client_attribution_metadata[checkout_session_id]': checkout_session_id,
        'client_attribution_metadata[merchant_integration_source]': 'checkout',
        'client_attribution_metadata[merchant_integration_version]': 'custom',
        'client_attribution_metadata[merchant_integration_subtype]': 'payment-element',
        'client_attribution_metadata[payment_method_selection_flow]': 'merchant_specified' if use_promo else 'automatic',
        'client_attribution_metadata[elements_session_id]': elements_session_id,
        'client_attribution_metadata[elements_session_config_id]': elements_config_id,
        'client_attribution_metadata[checkout_config_id]': checkout_config_id,
        'client_attribution_metadata[merchant_integration_additional_elements][0]': 'expressCheckout',
        'client_attribution_metadata[merchant_integration_additional_elements][1]': 'payment',
        'client_attribution_metadata[merchant_integration_additional_elements][2]': 'address',
    }
    return {key: value for key, value in fields.items() if value not in (None, '')}


def _custom_checkout_return_url(cs_id: str, processor_entity: str, checkout_client_secret: str = '') -> str:
    # Custom Checkout uses the Checkout Session client secret in the hosted
    # return URL fragment.  Keep it out of diagnostics/logging; it is only
    # placed into the request field required by Stripe's payment_pages API.
    fragment = ''
    secret = str(checkout_client_secret or '').strip()
    if '_secret_' in secret:
        fragment = secret.split('_secret_', 1)[1]
    query = urlencode({
        'returned_from_redirect': 'true',
        'ui_mode': 'custom',
        'return_url': _har_return_url(cs_id, processor_entity),
    })
    url = f'https://checkout.stripe.com/c/pay/{cs_id}?{query}'
    return url + (f'#{fragment}' if fragment else '')


def _stripe_custom_checkout_init(client: UpiHttpClient, *, cs_id: str, stripe_pk: str, stripe_js_id: str, proxy: str, use_promo: bool = True) -> dict[str, Any]:
    form = {
        # HAR（印度 UPI）：browser_locale=hi-IN、browser_timezone=Asia/Calcutta、
        # locale=hi-IN —— 全部取本会话指纹，不写死地区。
        'browser_locale': _stripe_browser_locale(client),
        'browser_timezone': _stripe_browser_timezone(client),
        'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1',
        'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1',
        'elements_session_client[elements_init_source]': 'custom_checkout',
        'elements_session_client[referrer_host]': 'chatgpt.com',
        'elements_session_client[stripe_js_id]': stripe_js_id,
        'elements_session_client[locale]': _stripe_browser_locale(client),
        'elements_session_client[is_aggregation_expected]': 'false',
        **_custom_checkout_elements_options(),
        'key': stripe_pk,
        '_stripe_version': STRIPE_VERSION,
    }
    # The source worker probes the base Stripe version first for UPI Custom
    # Checkout, then falls back to the beta/full version when required.
    for version in (STRIPE_VERSION, STRIPE_CUSTOM_VERSION):
        form['_stripe_version'] = version
        status, text, _ = client.request(
            'POST', f'https://api.stripe.com/v1/payment_pages/{cs_id}/init',
            headers=_stripe_headers(client), form=form, proxy=proxy,
        )
        if status == 200:
            data = _json_or_error(status, text, 'stripe custom checkout init')
            if isinstance(data, dict):
                # Keep the negotiated request version for elements/sessions;
                # Stripe expects the two calls to use the same revision.
                data['_request_stripe_version'] = version
            return data
        if status != 400:
            raise RuntimeError(f'stripe custom checkout init HTTP {status}')
    return _json_or_error(status, text, 'stripe custom checkout init')


def _stripe_custom_checkout_update(
    client: UpiHttpClient,
    *,
    cs_id: str,
    stripe_pk: str,
    stripe_js_id: str,
    elements: dict[str, Any],
    billing: dict[str, str],
    proxy: str,
    address_phase: str = 'full',
) -> dict[str, Any]:
    address = billing
    form = {'tax_region[country]': 'IN'}
    if address_phase in {'partial', 'full'}:
        form.update({
            'tax_region[line1]': address['line1'],
            'tax_region[city]': address['city'],
            'tax_region[postal_code]': address['postal_code'],
        })
    if address_phase == 'full':
        form['tax_region[state]'] = address['state']
        if address.get('line2'):
            form['tax_region[line2]'] = address['line2']
    form.update({
        **_custom_checkout_client_fields(
            stripe_js_id=stripe_js_id,
            elements=elements,
            checkout_session_id=cs_id,
            locale=_stripe_browser_locale(client),
        ),
        **_custom_checkout_elements_options(),
        'key': stripe_pk,
        '_stripe_version': STRIPE_CUSTOM_VERSION,
    })
    status, text, _ = client.request(
        'POST', f'https://api.stripe.com/v1/payment_pages/{cs_id}',
        headers=_stripe_headers(client), form=form, proxy=proxy,
    )
    return _json_or_error(status, text, 'stripe custom checkout tax update')


def _stripe_custom_checkout_fetch(
    client: UpiHttpClient,
    *,
    cs_id: str,
    stripe_pk: str,
    stripe_js_id: str,
    elements: dict[str, Any],
    proxy: str,
    use_promo: bool = True,
    step: Any = None,
) -> dict[str, Any]:
    """Fetch the current payment-page state with the exact Custom Checkout fields.

    Both supplied HARs perform this refresh after the ChatGPT tax/snapshot calls
    and again after approval.  In particular, root-level attribution fields are
    not valid on this GET endpoint.
    """
    params = {
        'key': stripe_pk,
        '_stripe_version': STRIPE_CUSTOM_VERSION,
        **_custom_checkout_client_fields(
            stripe_js_id=stripe_js_id,
            elements=elements,
            checkout_session_id=cs_id,
            use_promo=use_promo,
            locale=_stripe_browser_locale(client),
        ),
        **_custom_checkout_elements_options(),
    }
    params = {key: value for key, value in params.items() if not key.startswith('client_attribution_metadata')}
    url = f'https://api.stripe.com/v1/payment_pages/{cs_id}?' + urlencode(params)
    status, text, response_headers = client.request('GET', url, headers=_stripe_headers(client), proxy=proxy)
    diagnostic = _upi_http_diagnostic(status, response_headers)
    if status == 400 and 'checkout_not_active_session' in (text or ''):
        if callable(step):
            step('查询UPI支付设置状态', 'info', diagnostic + '; error=checkout_not_active_session')
        raise RuntimeError('checkout_not_active_session')
    try:
        data = _json_or_error(status, text, 'stripe custom checkout fetch')
    except RuntimeError:
        if callable(step):
            step('查询UPI支付设置状态', 'info', diagnostic)
        raise
    if callable(step):
        step('查询UPI支付设置状态', 'info', diagnostic + '; ' + _payment_page_poll_summary(data))
    return data


def _stripe_unknown_param(text: str) -> str:
    try:
        payload = json.loads(text or '{}')
    except json.JSONDecodeError:
        return ''
    error = payload.get('error') if isinstance(payload, dict) else None
    if not isinstance(error, dict) or str(error.get('code') or '').lower() != 'parameter_unknown':
        return ''
    return str(error.get('param') or '').strip()


def _stripe_custom_checkout_confirm(
    client: UpiHttpClient,
    *,
    cs_id: str,
    processor_entity: str,
    stripe_pk: str,
    stripe_js_id: str,
    elements: dict[str, Any],
    init_data: dict[str, Any],
    billing: dict[str, str],
    amount: int,
    checkout_client_secret: str,
    checkout_config_id: str,
    proxy: str,
    request_ids: dict[str, str] | None = None,
    use_promo: bool = True,
    step: Any = None,
) -> dict[str, Any]:
    init_checkout_config_id = str(init_data.get('config_id') or '').strip()
    elements_session_id = str(elements.get('session_id') or '').strip()
    elements_config_id = str(elements.get('config_id') or '').strip()
    # HAR：confirm 的 version 与 payment_user_agent 都用当前 stripe.js 的构建盐；
    # 顺序 = 环境变量 → init 响应自带 → 实时抓取 → 兜底常量（避免上游发版后落后）。
    runtime_version = str(
        os.environ.get('PAY153_UPI_STRIPE_RUNTIME_VERSION')
        or init_data.get('version')
        or ''
    ).strip()
    if not runtime_version:
        runtime_version = _stripe_runtime_version(client, proxy, STRIPE_RUNTIME_VERSION)
    if not runtime_version:
        runtime_version = STRIPE_RUNTIME_VERSION
    address = billing
    stable_ids = request_ids if isinstance(request_ids, dict) else {}
    missing_ids = [
        key for key in ('guid', 'muid', 'sid', 'stripe_js_id')
        if not str(stable_ids.get(key) or '').strip()
    ]
    if missing_ids:
        raise RuntimeError(
            'UPI 风控上下文生成失败：Stripe confirm 缺少当前 Firefox 支付页标识 '
            + ','.join(missing_ids)
        )
    if str(stable_ids['stripe_js_id']).strip() != str(stripe_js_id or '').strip():
        raise RuntimeError('UPI 风控上下文生成失败：Stripe confirm 的 stripe_js_id 与当前 Firefox 支付页不一致')
    form: dict[str, str] = {
        'guid': str(stable_ids['guid']),
        'muid': str(stable_ids['muid']),
        'sid': str(stable_ids['sid']),
        'expected_amount': str(int(amount)),
        'expected_payment_method_type': 'upi',
        'return_url': _custom_checkout_return_url(cs_id, processor_entity, checkout_client_secret),
        'init_checksum': str(init_data.get('init_checksum') or ''),
        'version': runtime_version,
        'key': stripe_pk,
        '_stripe_version': STRIPE_CUSTOM_VERSION,
        'payment_method_data[type]': 'upi',
        'payment_method_data[billing_details][name]': address['name'],
        'payment_method_data[billing_details][email]': address['email'],
        'payment_method_data[billing_details][address][line1]': address['line1'],
        'payment_method_data[billing_details][address][city]': address['city'],
        'payment_method_data[billing_details][address][postal_code]': address['postal_code'],
        'payment_method_data[billing_details][address][state]': address['state'],
        'payment_method_data[billing_details][address][country]': 'IN',
        'payment_method_data[payment_user_agent]': f'stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent',
        'payment_method_data[referrer]': 'https://chatgpt.com',
        'payment_method_data[time_on_page]': str(stable_ids.get('time_on_page') or random.randint(25000, 55000)),
    }
    # HAR 的 confirm 没有空 line2；只有真实填写了第二行地址才带上。
    if str(address.get('line2') or '').strip():
        form['payment_method_data[billing_details][address][line2]'] = str(address['line2']).strip()
    for key, value in _custom_checkout_client_fields(
        stripe_js_id=stripe_js_id,
        elements=elements,
        init_data=init_data,
        checkout_session_id=cs_id,
        use_promo=use_promo,
        locale=_stripe_browser_locale(client),
    ).items():
        form[key.replace('client_attribution_metadata[', 'payment_method_data[client_attribution_metadata][', 1)] = value
    # The browser sends the same attribution fields once inside the payment
    # method and once at the root of the payment-pages request.
    form.update(_custom_checkout_client_fields(
        stripe_js_id=stripe_js_id,
        elements=elements,
        init_data=init_data,
        checkout_session_id=cs_id,
        use_promo=use_promo,
    ))
    form['payment_method_data[client_attribution_metadata][merchant_integration_source]'] = 'elements'
    form['payment_method_data[client_attribution_metadata][merchant_integration_version]'] = '2021'
    form['payment_method_data[client_attribution_metadata][merchant_integration_subtype]'] = 'payment-element'
    form['payment_method_data[client_attribution_metadata][payment_intent_creation_flow]'] = 'deferred'
    form['payment_method_data[client_attribution_metadata][payment_method_selection_flow]'] = 'merchant_specified' if use_promo else 'automatic'
    form['payment_method_data[client_attribution_metadata][elements_session_id]'] = elements_session_id
    form['payment_method_data[client_attribution_metadata][elements_session_config_id]'] = elements_config_id
    form['payment_method_data[client_attribution_metadata][checkout_config_id]'] = init_checkout_config_id
    for index, name in enumerate(('expressCheckout', 'payment', 'address')):
        form[f'payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][{index}]'] = name
    form.update(_custom_checkout_elements_options())
    # The payment-page config advances after the address/tax update.  The
    # browser keeps the init config inside payment_method_data but sends the
    # latest config at the request root (verified in both supplied HARs).
    form['client_attribution_metadata[checkout_config_id]'] = str(
        checkout_config_id or init_checkout_config_id
    ).strip()
    # HAR 的根级 client_attribution_metadata 同时带 payment_intent_creation_flow=deferred
    # （payment_method_data 内那份已在上面设置）。
    form['client_attribution_metadata[payment_intent_creation_flow]'] = 'deferred'
    # Stripe's current payment-pages confirm includes these browser telemetry
    # fields when present in the init response. Keep them optional so older
    # accounts can still negotiate the request without leaking credentials.
    telemetry_env = {
        'js_checksum': 'PAY153_UPI_JS_CHECKSUM',
        'rv_timestamp': 'PAY153_UPI_RV_TIMESTAMP',
    }
    for key, env_key in telemetry_env.items():
        value = str(os.environ.get(env_key) or init_data.get(key) or '').strip()
        if value:
            form[key] = value
    # 只使用当前会话的验证字段；历史 HAR 的字段差异不能单独证明拒绝原因。
    # site_key 来自 Elements；保留既有可选字段处理。
    passive_captcha = elements.get('passive_captcha') if isinstance(elements, dict) else None
    captcha_site_key = str((passive_captcha or {}).get('site_key') or '').strip() if isinstance(passive_captcha, dict) else ''
    form.update(
        _upi_passive_captcha_fields(
            proxy,
            captcha_site_key,
            step,
            user_agent=str(getattr(client, 'ua', '') or ''),
            locale=str(getattr(client, 'locale', '') or ''),
            client=client,
        )
    )
    # Stripe includes the key even when no ekey is required.
    form.setdefault('passive_captcha_ekey', '')
    form['link_brand'] = 'link'
    for attempt in range(4):
        status, text, response_headers = client.request(
            'POST', f'https://api.stripe.com/v1/payment_pages/{cs_id}/confirm',
            headers=_stripe_headers(client), form=form, proxy=proxy,
        )
        if callable(step):
            step('提交Stripe支付确认', 'info', _upi_http_diagnostic(status, response_headers))
        if status == 200:
            return _json_or_error(status, text, 'stripe custom checkout confirm')
        unknown = _stripe_unknown_param(text)
        if status == 400 and unknown:
            removed = False
            for key in list(form):
                if key == unknown or key.startswith(unknown + '['):
                    form.pop(key, None)
                    removed = True
            if removed:
                continue
        return _json_or_error(status, text, 'stripe custom checkout confirm')
    raise RuntimeError('stripe custom checkout confirm failed after parameter negotiation')


def _checkout_approve_custom(
    client: UpiHttpClient,
    *,
    token: str,
    cs_id: str,
    processor_entity: str,
    proxy: str,
    referer: str,
    step: Any = None,
) -> dict[str, Any]:
    # 对齐 GoPay/GCash 链路文档 4.9:approve 需要 x-oai-is-client-observation 标记;
    # result=blocked/expired 时重领 SEN/SO 令牌立即重试一次(文档 5.3),仍失败才终态。
    # HAR #228：approve 是观察证明轮换后的阶段，这里按需刷新一次（仅浏览器捕获开启时动作）。
    _upi_refresh_browser_risk(client, access_token=token, proxy=proxy, page_url=referer, flow='', step=step)
    for attempt in range(2):
        headers = _target_headers(_chatgpt_headers(client, token), '/backend-api/payments/checkout/approve', referer)
        if client.client_observation:
            headers['x-oai-is-client-observation'] = client.client_observation
        risk_headers = _upi_sentinel_headers(proxy, client.device_id, 'checkout_session_approval', client, referer)
        headers.update(risk_headers)
        if callable(step):
            step(
                'ChatGPT订单风控审批',
                'info',
                f'正在提交第 {attempt + 1}/2 次审批；已生成本轮动态风控令牌（长度={len(risk_headers.get("openai-sentinel-token", ""))}）',
            )
        status, text, _ = client.request(
            'POST', 'https://chatgpt.com/backend-api/payments/checkout/approve',
            headers=headers,
            json_body={'checkout_session_id': cs_id, 'processor_entity': processor_entity},
            proxy=proxy,
        )
        _raise_if_upi_risk_blocked(status, text, 'checkout approve')
        if status >= 400:
            if attempt == 0 and status == 429:
                continue
            raise RuntimeError(f'UPI checkout approve HTTP {status}; {str(text)[:240]}')
        try:
            data = json.loads(text or '{}')
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'UPI checkout approve invalid JSON: {str(text)[:240]}') from exc
        result = str(data.get('result') or '').strip().lower()
        _raise_if_upi_risk_blocked(status, text, 'checkout approve', data)
        if result == 'approved':
            if callable(step):
                step('ChatGPT订单风控审批', 'ok', '订单审批通过')
            return data
        if attempt == 0 and result == 'expired':
            if callable(step):
                step('ChatGPT订单风控审批', 'warn', '首次审批令牌已过期，正在重新生成令牌并重试一次')
            continue
        raise RuntimeError(f'UPI checkout approve rejected; result={result or "unknown"}; {_checkout_confirm_diagnostic(data)}')
    raise RuntimeError('UPI checkout approve failed after risk-context retry')


def _find_upi_deep_link(value: Any) -> str:
    """Find the legacy UPI app deep link carried beside the QR payload."""
    if isinstance(value, str):
        match = re.search(r'upi://[^\s"\'<>]+', value, re.I)
        return match.group(0).rstrip(')],};') if match else ''
    if isinstance(value, dict):
        for key in ('upi_deep_link', 'deep_link', 'deeplink', 'upi_uri', 'uri'):
            candidate = _find_upi_deep_link(value.get(key))
            if candidate:
                return candidate
        for child in value.values():
            candidate = _find_upi_deep_link(child)
            if candidate:
                return candidate
    elif isinstance(value, (list, tuple)):
        for child in value:
            candidate = _find_upi_deep_link(child)
            if candidate:
                return candidate
    return ''


def _extract_upi_qr(payload: Any) -> dict[str, Any] | None:
    """Extract the Stripe UPI QR payload from a payment-page / payment-intent
    response (HAR 印度UPI.har #273): next_action.upi_handle_redirect_or_display_qr_code
    carries qr_code.image_url_svg/png plus hosted_instructions_url."""
    if not isinstance(payload, dict):
        return None

    def build(qr: dict[str, Any], inner: dict[str, Any] | None = None) -> dict[str, Any] | None:
        svg = str(qr.get('image_url_svg') or '').strip()
        png = str(qr.get('image_url_png') or qr.get('image_url') or '').strip()
        url = svg or png
        if not url:
            return None
        expires = qr.get('expires_at')
        return {
            'qr_url': url,
            'qr_image_url': url,
            'image_url_svg': svg,
            'image_url_png': png,
            'hosted_instructions_url': str((inner or {}).get('hosted_instructions_url') or '').strip(),
            'upi_deep_link': _find_upi_deep_link(inner or qr),
            'expires_at': int(expires) if isinstance(expires, (int, float)) else 0,
        }

    candidates: list[dict[str, Any]] = []
    for key in ('next_action',):
        na = payload.get(key)
        if isinstance(na, dict):
            candidates.append(na)
    for holder in ('setup_intent', 'payment_intent'):
        obj = payload.get(holder)
        if isinstance(obj, dict):
            na = obj.get('next_action')
            if isinstance(na, dict):
                candidates.append(na)
    for na in candidates:
        if str(na.get('type') or '') != 'upi_handle_redirect_or_display_qr_code':
            continue
        inner = na.get('upi_handle_redirect_or_display_qr_code')
        if not isinstance(inner, dict):
            continue
        qr = inner.get('qr_code')
        if isinstance(qr, dict):
            built = build(qr, inner)
            if built:
                return built

    # 兜底：实时响应的 next_action 嵌套层级/type 可能与 HAR 样本不一致，
    # 递归查找任何含 qr_code（带 image url）的 dict，避免因结构漂移漏推二维码。
    def walk(node: Any) -> dict[str, Any] | None:
        if isinstance(node, dict):
            qr = node.get('qr_code')
            if isinstance(qr, dict):
                built = build(qr, node)
                if built:
                    return built
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found:
                    return found
        return None

    return walk(payload)


def _is_upi_qr_image_url(value: Any) -> bool:
    """Return true for Stripe's QR image endpoint, never a copyable payment link."""
    text = str(value or '').strip().lower()
    return bool(text and 'qr.stripe.com/' in text and re.search(r'\.(?:png|svg)(?:\?|$)', text))


def _is_stripe_upi_hosted_instructions_url(value: Any) -> bool:
    text = str(value or '').strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
    except Exception:
        return False
    return (
        parsed.scheme.lower() == 'https'
        and (parsed.hostname or '').lower() == 'payments.stripe.com'
        and parsed.path.startswith('/upi/instructions/')
    )


def _upi_copyable_link(payload: Any, qr: dict[str, Any] | None = None) -> str:
    """Return the source-compatible UPI link, preferring hosted instructions."""
    qr = qr if isinstance(qr, dict) else (_extract_upi_qr(payload) or {})
    hosted = str(qr.get('hosted_instructions_url') or '').strip()
    if _is_stripe_upi_hosted_instructions_url(hosted):
        return hosted
    deep_link = str(qr.get('upi_deep_link') or '').strip()
    if deep_link.startswith('upi://'):
        return deep_link

    def find(node: Any) -> str:
        if isinstance(node, dict):
            candidate = str(node.get('hosted_instructions_url') or '').strip()
            if _is_stripe_upi_hosted_instructions_url(candidate):
                return candidate
            for child in node.values():
                found = find(child)
                if found:
                    return found
        elif isinstance(node, list):
            for child in node:
                found = find(child)
                if found:
                    return found
        return ''

    hosted = find(payload)
    if hosted:
        return hosted
    return _find_upi_deep_link(payload)


def _emit_upi_qr(qr: dict[str, Any], on_qr_update: Any, step: Any, client: UpiHttpClient | None = None) -> None:
    """Push a UPI QR to the engine/front-end via the GCash-style qr_refresh event.

    只透传 Stripe 的二维码 URL(qr.stripe.com)，由前端 <img> 直接懒加载；不再在
    Python 端把 PNG 下载成 base64。base64 会让 qr_refresh 事件膨胀到 MB 级，
    超过 Node 子进程行缓冲上限后被整行丢弃，连 qr_url/链接一起丢失。
    """
    url = str(qr.get('qr_url') or '').strip()
    if not url:
        return
    payload: dict[str, Any] = {**qr, 'event': 'qr_refresh', 'updated_at': time.time()}
    if callable(on_qr_update):
        try:
            on_qr_update(payload)
        except Exception:
            pass
    # 不写 step：__STAGE__ 事件会覆盖前端的 waiting_scan 展示状态。


def _emit_upi_paid(on_qr_update: Any, status: str, updated_at: float | None = None) -> None:
    if callable(on_qr_update):
        try:
            on_qr_update({
                'event': 'payment_status',
                'paid': True,
                'payment_status': status,
                'updated_at': updated_at or time.time(),
            })
        except Exception:
            pass


def _stripe_custom_checkout_poll(
    client: UpiHttpClient,
    *,
    cs_id: str,
    stripe_pk: str,
    stripe_js_id: str,
    elements: dict[str, Any],
    proxy: str,
    cancelled: Any = None,
    max_attempts: int = APPROVED_REDIRECT_POLL_ATTEMPTS,
    on_qr_update: Any = None,
    step: Any = None,
    use_promo: bool = True,
) -> dict[str, Any]:
    # HAR proof (UPI最新.har #244/#270): the browser's GET poll carries ONLY
    # elements_session_client / elements_options_client / key / _stripe_version.
    # client_attribution_metadata is a form field valid on POST /confirm and
    # /init; passing it on the GET poll makes Stripe reject with HTTP 400
    # parameter_unknown ("Received unknown parameter: client_attribution_metadata").
    if callable(step):
        step('查询UPI支付设置状态', 'run', '正在查询 Stripe 支付设置结果')
    last: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        _ensure_upi_not_cancelled(cancelled)
        # HAR timing (UPI最新.har #261→#270): the browser polls ~1.8s after
        # approve.  Polling immediately after approve can transiently hit
        # checkout_not_active_session because Stripe is still settling the
        # submission state, so give the first poll a short settling delay.
        if attempt == 1:
            time.sleep(1.5)
        try:
            last = _stripe_custom_checkout_fetch(
                client,
                cs_id=cs_id,
                stripe_pk=stripe_pk,
                stripe_js_id=stripe_js_id,
                elements=elements,
                proxy=proxy,
                use_promo=use_promo,
                step=step,
            )
        except RuntimeError as exc:
            if 'checkout_not_active_session' in str(exc) and attempt < max_attempts:
                time.sleep(1.5)
                continue
            raise
        # Reject terminal states before exposing a residual QR or hosted URL.
        _raise_for_upi_payment_page_failure(last)
        # UPI QR (HAR 印度UPI.har): next_action.upi_handle_redirect_or_display_qr_code
        # carries qr_code.image_url_svg; push it live so the front-end can show
        # it exactly like the GCash QR while we keep polling for the payment.
        qr = _extract_upi_qr(last)
        if qr:
            _emit_upi_qr(qr, on_qr_update, step, client)
        if _upi_copyable_link(last, qr):
            return last
        submission = last.get('submission_attempt') if isinstance(last, dict) else None
        # 到账 = submission succeeded（0 元授权即订阅已创建），或 setup_intent
        # 已完成；与 GCash 的 payment_status 事件语义一致。
        if isinstance(submission, dict) and str(submission.get('state') or '').lower() == 'succeeded':
            _emit_upi_paid(on_qr_update, 'succeeded')
            return last
        setup_intent = last.get('setup_intent') if isinstance(last, dict) else None
        if isinstance(setup_intent, dict) and str(setup_intent.get('status') or '').strip() == 'succeeded':
            _emit_upi_paid(on_qr_update, 'succeeded')
            return last
        if attempt < max_attempts:
            time.sleep(1.5)
    return last


def _has_active_plus(value: Any) -> bool:
    if isinstance(value, dict):
        if str(value.get('plan_type') or '').strip().lower() == 'plus' and value.get('is_delinquent') is not True:
            try:
                return int(value.get('seats_entitled', 1) or 0) >= 1
            except (TypeError, ValueError):
                return True
        return any(_has_active_plus(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_active_plus(child) for child in value)
    return False


def _upi_subscriptions_active(client: UpiHttpClient, token: str, proxy: str) -> bool:
    account_id = _jwt_account_id(token)
    if not account_id:
        return False
    headers = _target_headers(
        _chatgpt_headers(client, token),
        '/backend-api/subscriptions',
        'https://chatgpt.com/',
    )
    headers['Chatgpt-Account-Id'] = account_id
    try:
        status, text, _ = client.request(
            'GET',
            'https://chatgpt.com/backend-api/subscriptions?' + urlencode({'account_id': account_id}),
            headers=headers,
            proxy=proxy,
        )
        if status >= 400:
            return False
        return _has_active_plus(json.loads(text or '{}'))
    except Exception:
        return False


def _wait_upi_paid(
    client: UpiHttpClient,
    token: str,
    *,
    cs_id: str,
    stripe_pk: str,
    stripe_js_id: str,
    elements: dict[str, Any],
    proxy: str,
    timeout_seconds: float,
    on_qr_update: Any = None,
    cancelled: Any = None,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """Keep the successful checkout session alive until payment reconciliation."""
    deadline = time.monotonic() + max(30.0, float(timeout_seconds))
    last_state = 'pending'
    while time.monotonic() < deadline:
        _ensure_upi_not_cancelled(cancelled)
        if _upi_subscriptions_active(client, token, proxy):
            _emit_upi_paid(on_qr_update, 'plus_active')
            return {'paid': True, 'payment_status': 'plus_active', 'confirmed_via': 'subscriptions'}
        try:
            payload = _stripe_custom_checkout_fetch(
                client,
                cs_id=cs_id,
                stripe_pk=stripe_pk,
                stripe_js_id=stripe_js_id,
                elements=elements,
                proxy=proxy,
            )
            qr = _extract_upi_qr(payload)
            if qr:
                # 初次提链已经下载过 PNG。到账轮询只透传最新的 Stripe
                # 图片 URL，避免每 3 秒重复下载同一张二维码。
                _emit_upi_qr(qr, on_qr_update, None, None)
            submission = payload.get('submission_attempt') if isinstance(payload, dict) else None
            if isinstance(submission, dict):
                last_state = str(submission.get('state') or last_state).strip().lower()
            setup_intent = payload.get('setup_intent') if isinstance(payload, dict) else None
            setup_state = str(setup_intent.get('status') or '').strip().lower() if isinstance(setup_intent, dict) else ''
            if last_state in {'succeeded', 'complete'} or setup_state == 'succeeded':
                _emit_upi_paid(on_qr_update, 'succeeded')
                return {'paid': True, 'payment_status': 'succeeded', 'confirmed_via': 'stripe'}
        except Exception:
            pass
        sleep_fn(3.0)
    if callable(on_qr_update):
        try:
            on_qr_update({
                'event': 'payment_status',
                'paid': False,
                'payment_status': 'timeout',
                'qr_refresh_active': False,
                'updated_at': time.time(),
            })
        except Exception:
            pass
    return {'paid': False, 'payment_status': last_state, 'confirmed_via': 'timeout'}


def _start_upi_paid_watch(
    client: UpiHttpClient,
    token: str,
    *,
    cs_id: str,
    stripe_pk: str,
    stripe_js_id: str,
    elements: dict[str, Any],
    proxy: str,
    timeout_seconds: float,
    on_qr_update: Any = None,
    cancelled: Any = None,
) -> threading.Thread:
    def watch() -> None:
        try:
            _wait_upi_paid(
                client,
                token,
                cs_id=cs_id,
                stripe_pk=stripe_pk,
                stripe_js_id=stripe_js_id,
                elements=elements,
                proxy=proxy,
                timeout_seconds=timeout_seconds,
                on_qr_update=on_qr_update,
                cancelled=cancelled,
            )
        except InterruptedError:
            if callable(on_qr_update):
                try:
                    on_qr_update({'event': 'payment_status', 'paid': False, 'payment_status': 'canceled', 'qr_refresh_active': False, 'updated_at': time.time()})
                except Exception:
                    pass
        except Exception as exc:
            if callable(on_qr_update):
                try:
                    on_qr_update({
                        'event': 'payment_status',
                        'paid': False,
                        'payment_status': 'watch_error',
                        'qr_refresh_active': False,
                        'error': localize_upi_error(str(exc)),
                        'updated_at': time.time(),
                    })
                except Exception:
                    pass
        finally:
            client.close()

    thread = threading.Thread(target=watch, name='upi-paid-watch', daemon=False)
    thread.start()
    return thread


def _run_upi_custom_checkout(
    client: UpiHttpClient,
    *,
    token: str,
    step: Any,
    steps: list[dict[str, str]],
    cs_id: str,
    processor_entity: str,
    stripe_pk: str,
    checkout_client_secret: str,
    stripe_js_id: str,
    elements: dict[str, Any],
    init_data: dict[str, Any],
    checkout_config_id: str,
    amount: int,
    billing: dict[str, str],
    proxy: str,
    setup_proxies: list[str] | None = None,
    referer: str,
    cancelled: Any = None,
    on_qr_update: Any = None,
    use_promo: bool = True,
    promo_campaign: str | None = None,
    force_zero: bool = False,
    wait_paid: bool = False,
    paid_timeout: float = 900.0,
) -> dict[str, Any]:
    proxy_candidates = _setup_intent_proxy_candidates(proxy, setup_proxies)
    active_proxy = proxy_candidates[0]
    payment: dict[str, Any] = {}
    setup_last_error: Exception | None = None
    _upi_refresh_browser_risk(
        client,
        access_token=token,
        proxy=active_proxy,
        page_url=referer,
        flow='',
        max_age_s=UPI_OBSERVATION_MAX_AGE_S,
        step=step,
        stage='Stripe confirm前风控复核',
    )
    setup_request_ids = {
        **_locked_stripe_request_ids(client),
        'time_on_page': str(random.randint(25000, 55000)),
    }

    def submit_setup_intent(candidate: str) -> dict[str, Any]:
        step('提交Stripe支付确认', 'run', '正在提交 UPI 支付设置')
        confirmation = _stripe_custom_checkout_confirm(
            client, cs_id=cs_id, processor_entity=processor_entity, stripe_pk=stripe_pk,
            stripe_js_id=stripe_js_id, elements=elements, init_data=init_data,
            billing=billing, amount=amount, checkout_client_secret=checkout_client_secret,
            checkout_config_id=checkout_config_id,
            proxy=candidate,
            request_ids=setup_request_ids,
            use_promo=use_promo,
            step=step,
        )
        _raise_for_upi_payment_page_failure(confirmation)
        if _confirm_requires_approval(confirmation):
            _checkout_approve_custom(
                client, token=token, cs_id=cs_id, processor_entity=processor_entity,
                proxy=candidate, referer=referer, step=step,
            )
        return _stripe_custom_checkout_poll(
            client, cs_id=cs_id, stripe_pk=stripe_pk, stripe_js_id=stripe_js_id,
            elements=elements, proxy=candidate, cancelled=cancelled,
            max_attempts=UPI_PAYMENT_POLL_ATTEMPTS, on_qr_update=on_qr_update, step=step, use_promo=use_promo,
        )

    for retry_index in range(MAX_SETUP_INTENT_RETRIES + 1):
        _ensure_upi_not_cancelled(cancelled)
        if retry_index:
            next_index = retry_index % len(proxy_candidates)
            # 即使首条代理在池中间，也不能把同一条代理连续提交两次。
            if len(proxy_candidates) > 1 and proxy_candidates[next_index] == active_proxy:
                next_index = (next_index + 1) % len(proxy_candidates)
            active_proxy = proxy_candidates[next_index]
            step('提交Stripe支付确认', 'warn', 'SetupIntent 未生成有效支付方式；当前 Checkout 会话已终止')
        try:
            payment = submit_setup_intent(active_proxy)
            proxy = active_proxy
            break
        except Exception as exc:
            setup_last_error = exc
            if not _is_setup_intent_failure(exc) or retry_index >= MAX_SETUP_INTENT_RETRIES:
                raise
            # 不在当前会话继续提交；下一次由 Node 调度层重建完整 Checkout。
            continue
    step('提交Stripe支付确认', 'ok', 'Stripe 已接收 UPI 支付确认')
    # The paid HAR polls PaymentIntent directly; the zero-due HAR polls
    # SetupIntent. Prefer that object-specific endpoint when the payment-page
    # response exposes an id and client secret, while retaining the page
    # payload as the source of the hosted instructions URL.
    intent_obj = None
    intent_kind = ''
    for holder, kind in (('payment_intent', 'payment'), ('setup_intent', 'setup')):
        candidate = payment.get(holder) if isinstance(payment, dict) else None
        if isinstance(candidate, dict) and (candidate.get('id') or candidate.get('client_secret')):
            intent_obj, intent_kind = candidate, kind
            break
    if isinstance(intent_obj, dict):
        intent_id = str(intent_obj.get('id') or '').strip()
        intent_secret = str(intent_obj.get('client_secret') or '').strip()
        if intent_id and intent_secret:
            step('查询UPI支付意图状态', 'run', '正在查询 Stripe 支付意图状态')
            if intent_kind == 'payment':
                polled = _poll_stripe_payment_intent(
                    client, payment_intent_id=intent_id, client_secret=intent_secret,
                    stripe_pk=stripe_pk, steps=steps, proxy=proxy, cancelled=cancelled,
                    max_attempts=min(UPI_PAYMENT_POLL_ATTEMPTS, 20), on_qr_update=on_qr_update, step=step,
                    stripe_version=STRIPE_CUSTOM_VERSION,
                )
            else:
                polled = _poll_stripe_setup_intent(
                    client, setup_intent_id=intent_id, client_secret=intent_secret,
                    stripe_pk=stripe_pk, proxy=proxy, cancelled=cancelled,
                    max_attempts=min(UPI_PAYMENT_POLL_ATTEMPTS, 20), on_qr_update=on_qr_update, step=step,
                    stripe_version=STRIPE_CUSTOM_VERSION,
                )
            if isinstance(polled, dict):
                # Keep the page's hosted URL/QR if the intent endpoint omits
                # them, otherwise use the freshest status and QR payload.
                merged = dict(payment)
                merged.update(polled)
                if not _extract_upi_qr(merged):
                    merged['next_action'] = payment.get('next_action')
                payment = merged
    qr_data = _extract_upi_qr(payment) or {}
    # The source flow accepts the hosted instructions URL and, when Stripe
    # returns it, the legacy upi:// deep link. The QR image URL is never used
    # as the copy/open target.
    long_url = _upi_copyable_link(payment, qr_data)
    page_state = str((payment.get('submission_attempt') or {}).get('state') or payment.get('status') or '').strip()
    # 诊断：把提取到的链接与 next_action 原始值打到日志，方便直接判断。
    na_raw = payment.get('next_action') if isinstance(payment, dict) else None
    if callable(step):
        step('生成支付链接和二维码', 'info',
             '链接提取=' + (str(long_url)[:160] if long_url else '空')
             + '；next_action=' + (json.dumps(na_raw, ensure_ascii=False)[:300] if na_raw else '无'))
    if not long_url:
        raise RuntimeError('UPI Custom Checkout payment page 未生成可复制 UPI 链接：' + _payment_page_poll_summary(payment))
    # --force-zero 只允许继续探测，不代表优惠真的落地。不要把非 0 元
    # 结果打成成功，否则 Node 边界会随后拒绝并重试，前端就会看到
    # “gateway 成功”紧接着又回到“开始执行”的假象。
    zero_verified = (not use_promo) or int(amount) == 0
    link_accepted = bool(force_zero or zero_verified)
    detail = ('UPI 支付链接与二维码已生成' if zero_verified else
              ('UPI 支付链接与二维码已生成（--force-zero，最终金额以服务端为准）' if force_zero else
               f'支付链接已返回，但优惠未生效（应付金额={amount}），本轮不作为成功结果'))
    step('生成支付链接和二维码', 'ok' if link_accepted else 'warn', detail)
    payment_intent = payment.get('payment_intent') if isinstance(payment.get('payment_intent'), dict) else {}
    setup_intent = payment.get('setup_intent') if isinstance(payment.get('setup_intent'), dict) else {}
    intent = payment_intent or setup_intent
    intent_type = 'payment_intent' if payment_intent else ('setup_intent' if setup_intent else '')
    result = {
        'success': True,
        'ok': True,
        'long_url': long_url,
        'url': long_url,
        'provider_redirect_url': long_url,
        'cs_id': cs_id,
        'checkout_session_id': cs_id,
        'billing_country': 'IN',
        'currency': 'INR',
        'checkout_currency': 'INR',
        'amount': amount,
        'checkout_amount': amount,
        'promo_applied': (int(amount) == 0) if use_promo else None,
        'promo_requested': bool(use_promo),
        'promo_campaign_used': str(promo_campaign or UPI_PROMO_COUPON) if use_promo else '',
        'payment_method_types': ['upi'],
        'elements_session_id': str(elements.get('session_id') or ''),
        'payment_page_status': str(payment.get('status') or ''),
        'payment_object_type': intent_type,
        'payment_intent_id': _payment_intent_id(intent.get('id') or intent.get('client_secret')) if intent_type == 'payment_intent' else '',
        'setup_intent_id': str(intent.get('id') or '').strip() if intent_type == 'setup_intent' else '',
        'payment_intent_status': str(intent.get('status') or '') if intent_type == 'payment_intent' else '',
        'setup_intent_status': str(intent.get('status') or '') if intent_type == 'setup_intent' else '',
        'hosted_instructions_url': str(qr_data.get('hosted_instructions_url') or ''),
        'upi_deep_link': str(qr_data.get('upi_deep_link') or ''),
        'link_type': 'upi_deep_link' if str(qr_data.get('upi_deep_link') or '').startswith('upi://') and not qr_data.get('hosted_instructions_url') else 'upi_qr',
        'qr_url': str(qr_data.get('qr_url') or ''),
        'qr_image_url_png': str(qr_data.get('image_url_png') or ''),
        'qr_image_url_svg': str(qr_data.get('image_url_svg') or ''),
        'expires_at': qr_data.get('expires_at') or 0,
        'paid': page_state.lower() == 'succeeded',
        'payment_status': page_state,
        # A non-zero authoritative amount is terminal for this task.
        'reason_code': ('' if (not use_promo) or int(amount) == 0 else 'upi_promo_not_applied'),
        'retryable': False,
    }
    if wait_paid and not result['paid']:
        _start_upi_paid_watch(
            client,
            token,
            cs_id=cs_id,
            stripe_pk=stripe_pk,
            stripe_js_id=stripe_js_id,
            elements=elements,
            proxy=proxy,
            timeout_seconds=paid_timeout,
            on_qr_update=on_qr_update,
            cancelled=cancelled,
        )
        result['_watch_started'] = True
    return result


def _confirmation_token(
    client: UpiHttpClient,
    *,
    checkout_session_id: str,
    stripe_pk: str,
    billing: dict[str, str],
    stripe_js_id: str,
    elements: dict[str, Any],
    payment_method_types: list[str] | None,
    proxy: str,
    time_on_page_ms: int,
) -> dict[str, Any]:
    # payment_user_agent 里的版本必须与当前 stripe.js 一致（HAR 与 confirm 的 version 同值）。
    confirmation_runtime_version = _stripe_runtime_version(client, proxy, STRIPE_RUNTIME_VERSION)
    elements_session_id = str(elements.get('session_id') or '').strip()
    elements_config_id = str(elements.get('config_id') or '').strip()
    customer_obj = elements.get('customer') if isinstance(elements.get('customer'), dict) else {}
    customer_id = str(customer_obj.get('id') or customer_obj.get('customer') or '').strip()
    methods: list[str] = []
    for value in payment_method_types or []:
        method = str(value or '').strip().lower()
        if method and method not in methods:
            methods.append(method)
    if not methods:
        raise RuntimeError('UPI OAICS Checkout payment method list is empty')
    if 'upi' not in methods:
        raise RuntimeError('UPI OAICS Checkout does not offer upi: ' + ','.join(methods))
    request_ids = _locked_stripe_request_ids(client)
    if request_ids['stripe_js_id'] != str(stripe_js_id or '').strip():
        raise RuntimeError('UPI 风控上下文生成失败：OAICS stripe_js_id 与当前 Firefox 支付页不一致')
    form: dict[str, str] = {
        'payment_method_data[type]': 'upi',
        'payment_method_data[allow_redisplay]': 'limited',
        'payment_method_data[billing_details][name]': billing['name'],
        'payment_method_data[billing_details][email]': billing['email'],
        'payment_method_data[billing_details][address][line1]': billing['line1'],
        'payment_method_data[billing_details][address][city]': billing['city'],
        'payment_method_data[billing_details][address][country]': 'IN',
        'payment_method_data[billing_details][address][postal_code]': billing['postal_code'],
        'payment_method_data[billing_details][address][state]': billing['state'],
        'payment_method_data[payment_user_agent]': (
            f'stripe.js/{confirmation_runtime_version}; stripe-js-v3/{confirmation_runtime_version}; '
            'payment-element; deferred-intent'
        ),
        'payment_method_data[referrer]': 'https://chatgpt.com',
        'payment_method_data[time_on_page]': str(max(5000, int(time_on_page_ms))),
        'payment_method_data[client_attribution_metadata][checkout_session_id]': checkout_session_id,
        'payment_method_data[client_attribution_metadata][client_session_id]': stripe_js_id,
        'payment_method_data[client_attribution_metadata][merchant_integration_source]': 'elements',
        'payment_method_data[client_attribution_metadata][merchant_integration_subtype]': 'payment-element',
        'payment_method_data[client_attribution_metadata][merchant_integration_version]': '2021',
        'payment_method_data[client_attribution_metadata][payment_intent_creation_flow]': 'deferred',
        'payment_method_data[client_attribution_metadata][payment_method_selection_flow]': 'merchant_specified',
        'payment_method_data[client_attribution_metadata][elements_session_id]': elements_session_id,
        'payment_method_data[client_attribution_metadata][elements_session_config_id]': elements_config_id,
        'payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]': 'expressCheckout',
        'payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]': 'payment',
        'payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]': 'address',
        'payment_method_data[guid]': request_ids['guid'],
        'payment_method_data[muid]': request_ids['muid'],
        'payment_method_data[sid]': request_ids['sid'],
        'setup_future_usage': 'off_session',
        'mandate_data[customer_acceptance][type]': 'online',
        'mandate_data[customer_acceptance][online][infer_from_client]': 'true',
        'client_context[currency]': 'inr',
        'client_context[mode]': 'subscription',
        'client_attribution_metadata[client_session_id]': stripe_js_id,
        'client_attribution_metadata[merchant_integration_source]': 'elements',
        'client_attribution_metadata[merchant_integration_subtype]': 'payment-element',
        'client_attribution_metadata[merchant_integration_version]': '2021',
        'client_attribution_metadata[payment_intent_creation_flow]': 'deferred',
        'client_attribution_metadata[payment_method_selection_flow]': 'merchant_specified',
        'client_attribution_metadata[elements_session_id]': elements_session_id,
        'client_attribution_metadata[elements_session_config_id]': elements_config_id,
        'client_attribution_metadata[merchant_integration_additional_elements][0]': 'expressCheckout',
        'client_attribution_metadata[merchant_integration_additional_elements][1]': 'payment',
        'client_attribution_metadata[merchant_integration_additional_elements][2]': 'address',
        'set_as_default_payment_method': 'false',
        'key': stripe_pk,
        '_stripe_version': STRIPE_VERSION,
    }
    if str(billing.get('line2') or '').strip():
        form['payment_method_data[billing_details][address][line2]'] = str(billing['line2']).strip()
    if str(billing.get('phone') or '').strip():
        form['payment_method_data[billing_details][phone]'] = str(billing['phone']).strip()
    for index, method in enumerate(methods):
        form[f'client_context[payment_method_types][{index}]'] = method
    if customer_id:
        form['client_context[customer]'] = customer_id
    headers = _stripe_headers(client)
    headers['content-type'] = 'application/x-www-form-urlencoded'
    status, text, _ = client.request('POST', 'https://api.stripe.com/v1/confirmation_tokens', headers=headers, form=form, proxy=proxy)
    return _json_or_error(status, text, 'stripe confirmation token')


def _checkout_confirm_diagnostic(data: Any) -> str:
    """Return safe confirm diagnostics without echoing tokens/secrets."""
    if not isinstance(data, dict):
        return 'response_type=' + type(data).__name__
    parts = ['keys=' + ','.join(str(key) for key in list(data.keys())[:20])]
    for key in ('status', 'code', 'reason', 'blocked', 'risk'):
        value = data.get(key)
        if value not in (None, ''):
            if isinstance(value, (str, int, float, bool)):
                parts.append(f'{key}={str(value)[:120]}')
            else:
                parts.append(f'{key}=object')
    error = data.get('error')
    if isinstance(error, dict):
        parts.append('error_keys=' + ','.join(str(key) for key in list(error.keys())[:10]))
    elif error not in (None, ''):
        parts.append('error_present=true')
    return '; '.join(parts)


def _checkout_confirm_with_token(
    client: UpiHttpClient,
    *,
    token: str,
    cs_id: str,
    processor_entity: str,
    confirm_token: str,
    proxy: str,
    step: Any = None,
) -> dict[str, Any]:
    referer = f'https://chatgpt.com/checkout/{processor_entity}/{cs_id}'
    body = {'checkout_session_id': cs_id, 'confirm_token': confirm_token, 'selected_payment_method_type': 'upi'}
    _upi_refresh_browser_risk(
        client,
        access_token=token,
        proxy=proxy,
        page_url=referer,
        flow='checkout_session_approval',
        max_age_s=UPI_OBSERVATION_MAX_AGE_S,
        step=step,
        stage='OAICS confirm前风控复核',
    )
    for attempt in range(2):
        headers = _target_headers(_chatgpt_headers(client, token), '/backend-api/payments/checkout/confirm', referer)
        # GoPay's approval stage explicitly marks this as a client observation;
        # use the same header for UPI's checkout-session approval confirm.
        if client.client_observation:
            headers['x-oai-is-client-observation'] = client.client_observation
        risk_headers = _upi_sentinel_headers(proxy, client.device_id, 'checkout_session_approval', client, referer)
        headers.update(risk_headers)
        if callable(step):
            step('订单确认风控校验', 'info', f'第 {attempt + 1}/2 次校验；本轮动态风控令牌长度={len(risk_headers.get("openai-sentinel-token", ""))}')
        status, text, response_headers = client.request(
            'POST',
            'https://chatgpt.com/backend-api/payments/checkout/confirm',
            headers=headers,
            json_body=body,
            proxy=proxy,
        )
        try:
            data = json.loads(text or '{}')
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        _raise_if_upi_risk_blocked(status, text, 'checkout confirm', data)
        upstream_status = str(data.get('status') or '').strip().lower()
        blocked = upstream_status == 'blocked' or any(
            str(data.get(key) or '').strip().lower() in {'blocked', 'risk_blocked'}
            for key in ('blocked', 'risk', 'code', 'reason')
        )
        if status >= 400:
            # One fresh risk-context attempt is allowed for transient/expired
            # approval proofs. Do not loop or hide the final upstream reason.
            if attempt == 0 and status == 429:
                continue
            raise RuntimeError(
                f'UPI checkout confirm HTTP {status}; '
                f'{_checkout_confirm_diagnostic(data)}'
            )
        if blocked:
            raise RuntimeError(
                'UPI risk_blocked: checkout confirm blocked; '
                + _checkout_confirm_diagnostic(data)
            )
        if upstream_status and upstream_status not in {'success', 'ok'}:
            raise RuntimeError(
                'UPI checkout confirm rejected; '
                + _checkout_confirm_diagnostic(data)
            )
        return data
    raise RuntimeError('UPI checkout confirm failed after risk-context retry')


def _stripe_payment_intent_confirm(
    client: UpiHttpClient,
    *,
    payment_intent_id: str,
    client_secret: str,
    confirmation_token: str,
    stripe_pk: str,
    stripe_js_id: str,
    return_url: str,
    proxy: str,
) -> dict[str, Any]:
    form = {
        'return_url': return_url,
        'confirmation_token': confirmation_token,
        'key': stripe_pk,
        '_stripe_version': STRIPE_VERSION,
        'client_attribution_metadata[client_session_id]': stripe_js_id,
        'client_attribution_metadata[merchant_integration_source]': 'l1',
        'client_secret': client_secret,
    }
    headers = _stripe_headers(client)
    headers['content-type'] = 'application/x-www-form-urlencoded'
    status, text, _ = client.request('POST', f'https://api.stripe.com/v1/payment_intents/{payment_intent_id}/confirm', headers=headers, form=form, proxy=proxy)
    return _json_or_error(status, text, 'stripe payment intent confirm')


def _stripe_setup_intent_confirm(
    client: UpiHttpClient,
    *,
    setup_intent_id: str,
    client_secret: str,
    confirmation_token: str,
    stripe_pk: str,
    stripe_js_id: str,
    return_url: str,
    proxy: str,
) -> dict[str, Any]:
    # HAR entry 735 uses the same form as PaymentIntent confirmation, but
    # the endpoint is selected from Checkout's returned intent type.
    form = {
        'return_url': return_url,
        'confirmation_token': confirmation_token,
        'key': stripe_pk,
        '_stripe_version': STRIPE_VERSION,
        'client_attribution_metadata[client_session_id]': stripe_js_id,
        'client_attribution_metadata[merchant_integration_source]': 'l1',
        'client_secret': client_secret,
    }
    headers = _stripe_headers(client)
    headers['content-type'] = 'application/x-www-form-urlencoded'
    status, text, _ = client.request(
        'POST', f'https://api.stripe.com/v1/setup_intents/{setup_intent_id}/confirm',
        headers=headers, form=form, proxy=proxy,
    )
    return _json_or_error(status, text, 'stripe setup intent confirm')


def _raise_for_upi_intent_failure(payload: dict[str, Any]) -> None:
    state = str(payload.get('status') or '').strip().lower()
    if state not in {'canceled', 'requires_payment_method'}:
        return
    error = payload.get('last_setup_error') or payload.get('last_payment_error') or {}
    # Preserve machine-readable error codes without logging the payment
    # method, billing data or client_secret embedded in the error object.
    codes = []
    if isinstance(error, dict):
        for key in ('code', 'decline_code'):
            value = str(error.get(key) or '')
            if re.fullmatch(r'[a-zA-Z0-9_]{1,100}', value):
                codes.append(f'{key}={value}')
    raise RuntimeError('UPI Stripe intent failed: status=' + state + ('; ' + '; '.join(codes) if codes else ''))


def _poll_stripe_payment_intent(
    client: UpiHttpClient,
    *,
    payment_intent_id: str,
    client_secret: str,
    stripe_pk: str,
    steps: list[dict[str, str]],
    proxy: str,
    cancelled: Any = None,
    max_attempts: int = 10,
    on_qr_update: Any = None,
    step: Any = None,
    stripe_version: str = STRIPE_VERSION,
) -> dict[str, Any]:
    params = {'is_stripe_sdk': 'false', 'client_secret': client_secret, 'key': stripe_pk, '_stripe_version': stripe_version}
    url = f'https://api.stripe.com/v1/payment_intents/{payment_intent_id}?' + urlencode(params)
    last: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        _ensure_upi_not_cancelled(cancelled)
        headers = _stripe_headers(client)
        headers['content-type'] = 'application/x-www-form-urlencoded'
        status, text, _ = client.request('GET', url, headers=headers, proxy=proxy)
        last = _json_or_error(status, text, 'stripe payment intent poll')
        _raise_for_upi_intent_failure(last)
        # UPI 支付意图进入 requires_action 后，next_action 携带二维码
        # （HAR 印度UPI.har #273/#285）；每次轮询响应都可能刷新 expires_at，
        # 这里持续把最新二维码推给前端，直到 status=succeeded（到账）。
        qr = _extract_upi_qr(last)
        if qr:
            _emit_upi_qr(qr, on_qr_update, step, client)
        state = str(last.get('status') or '').strip()
        if state == 'succeeded':
            _emit_upi_paid(on_qr_update, 'succeeded')
            steps.append({'name': 'payment intent poll', 'status': 'ok', 'detail': f'status={state}（到账）'})
            return last
        if _upi_copyable_link(last, qr):
            return last
        if state in {'succeeded', 'processing', 'requires_action'}:
            steps.append({'name': 'payment intent poll', 'status': 'info', 'detail': f'attempt {attempt}/{max_attempts}; status={state}'})
        if attempt < max_attempts:
            time.sleep(1.5)
    return last


def _poll_stripe_setup_intent(
    client: UpiHttpClient,
    *,
    setup_intent_id: str,
    client_secret: str,
    stripe_pk: str,
    proxy: str,
    cancelled: Any = None,
    max_attempts: int = 10,
    on_qr_update: Any = None,
    step: Any = None,
    stripe_version: str = STRIPE_VERSION,
) -> dict[str, Any]:
    """Poll the zero-due UPI SetupIntent endpoint seen in UPI.har.

    Custom Checkout returns the QR on the payment-page response first, but
    the browser then refreshes ``/v1/setup_intents/{id}`` until the mandate is
    complete. This mirrors that final leg and works with either a client
    secret returned at the top level or nested in the intent object.
    """
    params = {
        'client_secret': client_secret,
        'is_stripe_sdk': 'false',
        'key': stripe_pk,
        '_stripe_version': stripe_version,
    }
    url = f'https://api.stripe.com/v1/setup_intents/{setup_intent_id}?' + urlencode(params)
    last: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        _ensure_upi_not_cancelled(cancelled)
        status, text, _ = client.request('GET', url, headers=_stripe_headers(client), proxy=proxy)
        last = _json_or_error(status, text, 'stripe setup intent poll')
        _raise_for_upi_intent_failure(last)
        qr = _extract_upi_qr(last)
        if qr:
            _emit_upi_qr(qr, on_qr_update, step, client)
        state = str(last.get('status') or '').strip()
        if state in {'succeeded', 'canceled'}:
            if state == 'succeeded':
                _emit_upi_paid(on_qr_update, state)
            return last
        if _upi_copyable_link(last, qr):
            return last
        if callable(step):
            qr_hint = '有' if qr else '无'
            keys = ','.join(list(last.keys())[:10]) if isinstance(last, dict) else '?'
            na = last.get('next_action') if isinstance(last, dict) else None
            na_type = str(na.get('type') or '') if isinstance(na, dict) else ''
            # 完整 dump next_action，方便直接看到链接/二维码字段的真实值与层级。
            na_dump = json.dumps(na, ensure_ascii=False)[:500] if na else '无'
            step('查询0元支付状态', 'info', f'查询 {attempt}/{max_attempts}；状态={_upi_status_cn(state)}；二维码={qr_hint}；next_action_type={na_type or "无"}；keys=[{keys}]')
            step('查询0元支付状态', 'info', f'next_action原始值={na_dump}')
        if attempt < max_attempts:
            time.sleep(1.5)
    return last


def _fetch_checkout_page_data(client: UpiHttpClient, token: str, *, cs_id: str, processor_entity: str, proxy: str) -> tuple[int, str]:
    """Replay the HAR checkout route data request as a prerequisite fallback."""
    route = f'/checkout/{processor_entity}/{cs_id}.data?_routes=routes%2Fcheckout.%24entity.%24checkoutId'
    url = 'https://chatgpt.com' + route
    headers = _target_headers(_chatgpt_headers(client, token), route, 'https://chatgpt.com/')
    try:
        return client.request('GET', url, headers=headers, proxy=proxy, timeout=35.0)[:2]
    except Exception:
        return 0, ''


def _fetch_upi_oaics_state(
    client: UpiHttpClient,
    token: str,
    *,
    cs_id: str,
    processor_entity: str,
    proxy: str,
) -> dict[str, Any]:
    """Refresh an ``oaics_`` Checkout state before creating Elements.

    MoMo performs this refresh when the initial create response does not yet
    contain its wallet method. UPI uses the same authenticated Checkout state
    endpoint, but keeps the request in the UPI client so the original cookies,
    device id, Sentinel context and India egress are preserved.
    """
    if not str(cs_id or '').startswith('oaics_'):
        return {}
    route = f'/backend-api/payments/checkout/{processor_entity}/{cs_id}'
    headers = _target_headers(
        _chatgpt_headers(client, token),
        route,
        f'https://chatgpt.com/checkout/{processor_entity}/{cs_id}',
    )
    status, text, _ = client.request(
        'GET',
        'https://chatgpt.com' + route,
        headers=headers,
        proxy=proxy,
        timeout=45.0,
    )
    if status >= 400:
        raise RuntimeError(f'UPI OAICS state refresh HTTP {status}; {str(text)[:240]}')
    data = _json_or_error(status, text, 'UPI OAICS state refresh')
    return data if isinstance(data, dict) else {}


# GCash 已验证的 cpmt 绕过链路(对齐 gcash_engine/gcash_custom_flow.py):
#   POST /checkout/confirm body={"checkout_session_id","selected_payment_method_type": cpmt}
#   POST /checkout/custom_payment_method/start body={"checkout_session_id","custom_payment_method_type_id": cpmt}
#   → start 响应 status=requires_action + next_action.url = 支付机构跳转 → 直接产出链接
#   blocked 时重领 SEN/SO 立即重试一次。
# UPI 会话当前 custom_payment_methods=null(OpenAI 未给 UPI 开 cpmt 通道),
# 该分支默认不触发;若上游将来返回 cpmt_... 类型(含 upi)则自动走此绕过链路。
_UPI_CPMT_CONFIRM_URL = 'https://chatgpt.com/backend-api/payments/checkout/confirm'
_UPI_CPMT_START_URL = 'https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start'


def _upi_custom_payment_method_id(custom_payment_methods: Any) -> str:
    """Only return an UPI-related CPMT id from the checkout response; never substitute."""
    if not isinstance(custom_payment_methods, list):
        return ''
    for item in custom_payment_methods:
        if isinstance(item, dict):
            cid = str(item.get('id') or '').strip()
            ctype = str(item.get('type') or '').strip()
        else:
            cid = str(item or '').strip()
            ctype = cid
        if cid.startswith('cpmt_') and ('upi' in ctype.lower() or 'upi' in cid.lower()):
            return cid
    return ''


def _run_upi_cpmt_flow(
    client: UpiHttpClient, token: str, step: Any, steps: list[dict[str, str]], *,
    cs_id: str, processor_entity: str, cpm_id: str, referer: str, proxy: str,
    amount: int | None, promo_marker: str, promo_campaign: str,
    on_qr_update: Any = None, cancelled: Any = None,
) -> dict[str, Any]:
    """Run the server-advertised UPI custom payment method."""
    _ensure_upi_not_cancelled(cancelled)
    _require_zero_due(amount, 'UPI CPMT Checkout', force=False)
    if promo_marker != 'applied':
        raise RuntimeError(f'upi promo not applied: source=UPI CPMT Checkout; marker={promo_marker or "unknown"}')
    for attempt in range(2):
        _upi_refresh_browser_risk(
            client,
            access_token=token,
            proxy=proxy,
            page_url=referer,
            flow='checkout_session_approval',
            max_age_s=UPI_OBSERVATION_MAX_AGE_S,
            step=step,
            stage='UPI CPMT confirm前风控复核',
        )
        # 风控上下文:新领 SEN/SO(flow=checkout_session_approval)+ attestation + observation
        risk_headers = _upi_sentinel_headers(proxy, client.device_id, 'checkout_session_approval', client, referer)
        if client.client_observation:
            risk_headers['x-oai-is-client-observation'] = client.client_observation
        if client.attestation:
            risk_headers['oai-web-deployment-attestation'] = client.attestation

        confirm_headers = _target_headers(_chatgpt_headers(client, token), '/backend-api/payments/checkout/confirm', referer)
        confirm_headers.update(risk_headers)
        st, text, _ = client.request(
            'POST', _UPI_CPMT_CONFIRM_URL,
            headers=confirm_headers,
            json_body={'checkout_session_id': cs_id, 'selected_payment_method_type': cpm_id},
            proxy=proxy,
        )
        _raise_if_upi_risk_blocked(st, text, 'UPI cpmt confirm')
        if st >= 400:
            if attempt == 0 and st == 429:
                continue
            raise RuntimeError(f'UPI cpmt confirm HTTP {st}; {str(text)[:240]}')
        try:
            confirm_data = json.loads(text or '{}')
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'UPI cpmt confirm invalid JSON: {str(text)[:240]}') from exc
        cstatus = str(confirm_data.get('status') or '').strip().lower()
        _raise_if_upi_risk_blocked(st, text, 'UPI cpmt confirm', confirm_data)
        if cstatus == 'blocked':
            raise RuntimeError('UPI risk_blocked: cpmt confirm; ' + _checkout_confirm_diagnostic(confirm_data))
        if cstatus not in {'success', 'ok', ''}:
            raise RuntimeError(f'UPI cpmt confirm status={cstatus or "unknown"}; ' + _checkout_confirm_diagnostic(confirm_data))
        step('确认UPI自定义通道', 'ok', f'ChatGPT 已确认 UPI 通道（{cpm_id}）')

        start_headers = _target_headers(_chatgpt_headers(client, token), '/backend-api/payments/checkout/custom_payment_method/start', referer)
        start_headers.update(risk_headers)
        st2, text2, _ = client.request(
            'POST', _UPI_CPMT_START_URL,
            headers=start_headers,
            json_body={'checkout_session_id': cs_id, 'custom_payment_method_type_id': cpm_id},
            proxy=proxy,
        )
        _raise_if_upi_risk_blocked(st2, text2, 'UPI cpmt start')
        if st2 >= 400:
            if attempt == 0 and st2 == 429:
                continue
            raise RuntimeError(f'UPI cpmt start HTTP {st2}; {str(text2)[:240]}')
        try:
            start_data = json.loads(text2 or '{}')
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'UPI cpmt start invalid JSON: {str(text2)[:240]}') from exc
        sstatus = str(start_data.get('status') or '').strip().lower()
        _raise_if_upi_risk_blocked(st2, text2, 'UPI cpmt start', start_data)
        if sstatus == 'blocked':
            raise RuntimeError('UPI risk_blocked: cpmt start; ' + _checkout_confirm_diagnostic(start_data))
        target = _upi_copyable_link(start_data)
        if target:
            step('启动UPI自定义通道', 'ok', f'已获得支付跳转；状态={_upi_status_cn(sstatus or "requires_action")}')
            return {
                'success': True,
                'ok': True,
                'long_url': target,
                'url': target,
                'provider_redirect_url': target,
                'cs_id': cs_id,
                'checkout_session_id': cs_id,
                'billing_country': 'IN',
                'currency': 'INR',
                'checkout_currency': 'INR',
                'amount': int(amount),
                'checkout_amount': int(amount),
                'promo_applied': True,
                'promo_requested': True,
                'promo_campaign_used': str(promo_campaign or UPI_PROMO_COUPON),
                'payment_method_types': ['upi'],
                'cpmt_id': cpm_id,
                'qr_url': '',
                'paid': False,
                'payment_status': sstatus or 'requires_action',
            }
        if attempt == 0:
            continue
        raise RuntimeError(f'UPI cpmt start 未返回跳转地址(status={sstatus or "?"}); keys={list(start_data)[:12]}')
    raise RuntimeError('UPI cpmt flow failed after risk-context retry')


def _run_upi_attempt(client: UpiHttpClient, token: str, step: Any, steps: list[dict[str, str]], *, promotion_proxy: str, billing: dict[str, str], setup_proxies: list[str] | None = None, use_promo: bool = True, promo_campaign: str | None = None, on_qr_update: Any = None, cancelled: Any = None, checkout_ui_mode: str = 'auto', force_payment_method: bool = False, force_zero: bool = False, wait_paid: bool = False, paid_timeout: float = 900.0, upi_rail: str = 'upi', upi_setup_v2: bool = False, require_server_upi_mandate: bool = False, requested_upi_mandate: dict[str, Any] | None = None, upi_har_context: bool = False) -> dict[str, Any]:
    _ensure_upi_not_cancelled(cancelled)
    india_base = client.base_proxy
    checkout_proxy = india_base
    promo_page_url = _upi_promo_page_url(promo_campaign or UPI_PROMO_COUPON) if use_promo else 'https://chatgpt.com/checkout'
    try:
        warm_status, cookie_header = _warmup_upi_session(client, token, promo_page_url, checkout_proxy)
        step('页面预热', 'ok', f'ChatGPT 页面访问成功（HTTP {warm_status}），已同步 {len(cookie_header.split(";"))} 个 Cookie')
    except Exception as exc:
        # A warmup failure is diagnostic only; the API request still gets the
        # stable oai-did Cookie fallback, matching GoPay's graceful fallback.
        client.refresh_cookie_header()
        step('页面预热', 'warn', '当前代理未能完成页面预热，将继续尝试生成风控上下文')
    risk = _capture_upi_browser_risk(
        client,
        access_token=token,
        proxy=checkout_proxy,
        page_url=promo_page_url,
        flow='',
    )
    if not _upi_risk_context_ok(client, risk):
        diagnostic = str(risk.get('error') or '').strip()
        # 协议模式能接受"页面没下发部署证明"：源站 git5 一开始也没有 attestation
        # 头，只有浏览器 rail 才有。因此这里的措辞按 rail 区分，避免把协议模式的
        # 正常缺省描述成浏览器故障。
        if _upi_protocol_client(client):
            reason = '纯协议模式缺少签名观察证明或 Stripe 标识'
        else:
            reason = '当前 Firefox 会话缺少 signed observation/attestation/Cookie'
        raise RuntimeError('UPI 风控上下文生成失败：' + reason + (f'; {diagnostic[:240]}' if diagnostic else ''))
    if _upi_protocol_client(client):
        step(
            '风控上下文',
            'ok',
            'macOS 纯协议上下文已生成：部署证明='
            + ('已获取' if client.attestation else '未获取（沿用无证明直连）')
            + f'，Stripe 标识=4/4，Cookie={risk.get("browser_cookie_count") or 0} 个',
        )
    else:
        step(
            '风控上下文',
            'ok',
            f'Firefox 当前会话已获取动态部署证明、signed observation 和 {risk.get("browser_cookie_count") or 0} 个 Cookie',
        )
    headers = _chatgpt_headers(client, token)
    headers['referer'] = promo_page_url
    risk_headers = _upi_sentinel_headers(checkout_proxy, client.device_id, 'chatgpt_checkout', client, promo_page_url)
    headers.update(risk_headers)
    sentinel_len = len(str(risk_headers.get('openai-sentinel-token') or ''))
    step(
        'Sentinel风控证明',
        'ok' if sentinel_len else 'warn',
        ('已生成并携带 openai-sentinel-token' if sentinel_len else 'Sentinel令牌未生成，将由上游返回结果决定是否重试')
        + (f'（长度={sentinel_len}）' if sentinel_len else ''),
    )
    _sentinel_ping(client, token, checkout_proxy, promo_page_url)
    # 对齐 txt 文档 Step 0 与 UPI最新.har #27/#28:定价配置在建单之前获取
    _warmup_india_client(client, token, checkout_proxy)
    step('浏览器指纹', 'ok', f'印度 {client.kernel.capitalize()} {client.chrome_full}；语言={client.locale}；设备号={client.device_id[:8]}…；动态部署证明=已获取')
    _human_pause('short')
    # Auto uses the native server-selected session. Explicit modes retain
    # the older payment_pages/deferred routing choices.
    _requested_ui_mode = str(checkout_ui_mode or 'auto').strip().lower()
    # UPI 只有两条真实路径：custom（cs_live_ → Stripe payment_pages，即
    # MoMo 所称 hosted）与 deferred（oaics_ → Elements）。因此 hosted 归入
    # custom/payment_pages，避免把不存在的 checkout_ui_mode 值发给上游。
    _build_ui_mode = 'custom' if _requested_ui_mode in ('auto', 'custom', 'hosted') else _requested_ui_mode
    if _requested_ui_mode == 'auto':
        step('选择UPI支付通道', 'info', 'checkout_ui_mode=auto：按建单响应自动识别 oaics(custom)/cs_live_(hosted) 提链路径')
    checkout_body = {
        'entry_point': 'all_plans_pricing_modal',
        'plan_name': 'chatgptplusplan',
        'billing_details': {'country': 'IN', 'currency': 'INR'},
        # Enter through the same campaign URL as the web checkout. The backend
        # derives part of the promotion context from this navigation as well as
        # from the explicit campaign object below.
        # The web checkout carries the campaign on the create request. Do not
        # create a full-price session and mutate it through the old
        # legacy session-update route; that route is not part of the HAR
        # flow and can leave the session/payment intent inconsistent.
    }
    # The native OAICS request (HAR entry 438) leaves UI selection to the
    # server. Only an explicit caller choice sends checkout_ui_mode.
    if _requested_ui_mode != 'auto':
        checkout_body['checkout_ui_mode'] = _build_ui_mode

    if use_promo:
        checkout_body['promo_campaign'] = {
            'promo_campaign_id': str(promo_campaign or UPI_PROMO_COUPON),
            'is_coupon_from_query_param': False,
        }
    ch = dict(headers)
    ch['x-openai-target-path'] = '/backend-api/payments/checkout'
    ch['x-openai-target-route'] = '/backend-api/payments/checkout'
    status, text, _ = client.request('POST', 'https://chatgpt.com/backend-api/payments/checkout', headers=ch, json_body=checkout_body, proxy=checkout_proxy)
    # Only classify a create failure as a promotion/eligibility failure when
    # the upstream body actually names promotion/coupon eligibility.  A plain
    # 403 remains a risk-control/account/network error rather than being
    # incorrectly reported as "no coupon".
    if use_promo and status >= 400 and _upi_promo_error(text):
        raise RuntimeError('UPI 优惠压0失败：账号无优惠资格')
    checkout = _json_or_error(status, text, 'checkout create')
    cs_id = _first_string(checkout, ['checkout_session_id', 'session_id', 'id'])
    if not _is_checkout_session_id(cs_id):
        raise RuntimeError(f'checkout missing oaics session id: {text[:240]}')
    processor_entity = _first_string(checkout, ['processor_entity', 'processorEntity']) or 'openai_llc'
    # HAR order: after POST /payments/checkout the page loads its .data route
    # before Stripe Elements.  Replay it as a fallback because some upstream
    # responses put Stripe prerequisites in checkoutConfig rather than the
    # POST response's top level.
    data_status, checkout_data_text = _fetch_checkout_page_data(
        client, token, cs_id=cs_id, processor_entity=processor_entity, proxy=checkout_proxy
    )
    checkout_ui_mode = _checkout_prerequisite_string(
        checkout, text, ('checkout_ui_mode', 'checkout_mode')
    ).strip().lower()
    # ── oaics_ vs cs_live_ 自动分流（对齐 MoMo auto）──
    #   cs_live_  → hosted / Stripe Custom Checkout (payment_pages) 路径；
    #   oaics_    → deferred-intent Elements 路径（customer_session_client_secret）。
    # 会话 id 前缀是最可靠的信号：checkout_ui_mode 字符串字段可能为 custom
    # 但会话却是 cs_live_，反之亦然。显式指定 custom/hosted/deferred 时仍
    # 优先用户选择；auto 时按前缀自动识别，识别失败回退到响应字段。
    if _requested_ui_mode == 'auto':
        if cs_id.startswith('cs_'):
            detected_mode = 'custom'
            step('选择UPI支付通道', 'info', '检测到 cs_ 会话，走 hosted / Stripe Custom Checkout (payment_pages) 提链路径')
        elif cs_id.startswith('oaics_'):
            detected_mode = 'deferred'
            step('选择UPI支付通道', 'info', '检测到 oaics_ 会话，走 deferred-intent Elements 提链路径')
        else:
            detected_mode = ''
        if detected_mode:
            checkout_ui_mode = detected_mode
    else:
        checkout_ui_mode = _build_ui_mode
    checkout_client_secret = _checkout_prerequisite_string(
        checkout, text, ('client_secret', 'checkout_session_client_secret')
    )
    stripe_pk = _checkout_prerequisite_string(
        checkout, text, ('publishable_key', 'stripe_publishable_key')
    )
    customer_session_secret = _checkout_prerequisite_string(
        checkout, text, ('customer_session_client_secret', 'customer_session_secret')
    )
    if not stripe_pk:
        stripe_pk = _named_string_from_text(checkout_data_text, ('publishable_key', 'stripe_publishable_key'))
    if not checkout_client_secret:
        checkout_client_secret = _named_string_from_text(
            checkout_data_text, ('client_secret', 'checkout_session_client_secret')
        )
    if not checkout_ui_mode:
        checkout_ui_mode = 'custom' if checkout_client_secret else 'deferred'
    is_custom_checkout = checkout_ui_mode == 'custom'
    # Custom Checkout intentionally has no CustomerSession secret.  The
    # checkout-session client_secret is the relevant response field; the
    # legacy deferred-intent branch still requires CustomerSession as before.
    # For OAICS, the initial create response may omit
    # customer_session_client_secret; MoMo refreshes the authenticated state
    # endpoint before treating that as a hard prerequisite.  Custom Checkout
    # still requires its checkout client secret immediately.
    if not stripe_pk or (is_custom_checkout and not checkout_client_secret):
        keys = ','.join(str(key) for key in checkout.keys())[:240]
        data_hint = f'; page_data_http={data_status}' if data_status else ''
        tag_hint = str(checkout.get('tag') or '').strip()
        required = 'publishable_key/client_secret' if is_custom_checkout else 'publishable_key/customer_session_client_secret'
        raise RuntimeError(
            'UPI Checkout payment prerequisites missing: '
            f'{required}; tag={tag_hint or "none"}; checkout_keys={keys or "none"}{data_hint}'
        )
    # Custom Checkout advertises UPI from Stripe's later elements/sessions
    # response, so an absent top-level list is not an error at this stage.
    if is_custom_checkout and _payment_method_types(checkout):
        _ensure_upi_offered(
            checkout, 'IN bootstrap',
            force=force_payment_method,
            on_warn=(lambda phase, methods: step(
                '选择UPI支付通道', 'warn',
                f'payment_method_types 不含 upi（={",".join(methods) or "none"}），已按 --force-payment-method 强制提交 upi'
            )) if force_payment_method else None,
        )
    step('创建支付订单', 'ok', f'印度 UPI 订单已创建；会话={cs_id[:24]}…；处理方={processor_entity}')
    promo_marker = _upi_promo_marker(checkout) if use_promo else 'not_requested'
    step(
        '应用0元优惠',
        'ok' if promo_marker == 'applied' else 'info',
        f'优惠活动={promo_campaign or UPI_PROMO_COUPON if use_promo else "未提交"}；订单优惠状态={_upi_status_cn(promo_marker)}',
    )
    _human_pause('med')

    # The supplied HAR has no separate post-create promotion request.  Keep the
    # same IN checkout session, identity and India egress for taxes and the
    # deferred-intent chain; pool 2 remains the VN eligibility precheck
    # transport, while pool 1 handles the actual UPI payment flow.
    referer = f'https://chatgpt.com/checkout/{processor_entity}/{cs_id}'

    # GCash 已验证的 cpmt 绕过链路自动适配:若本会话的 custom_payment_methods
    # 暴露了 UPI 相关 cpmt(如 cpmt_...upi...),走 ChatGPT 侧 confirm+start,
    # 不创建 Stripe SetupIntent,避开 Stripe 支付设置风控(generic_decline)。
    cpm_id = _upi_custom_payment_method_id(checkout.get('custom_payment_methods'))
    if cpm_id:
        step('选择UPI支付通道', 'info', f'订单提供自定义 UPI 通道 {cpm_id}，开始生成支付跳转')
        return _run_upi_cpmt_flow(
            client, token, step, steps,
            cs_id=cs_id, processor_entity=processor_entity, cpm_id=cpm_id,
            referer=referer, proxy=india_base or checkout_proxy,
            amount=_amount_from_payload(checkout),
            promo_marker=promo_marker,
            promo_campaign=str(promo_campaign or UPI_PROMO_COUPON),
            on_qr_update=on_qr_update, cancelled=cancelled,
        )

    return _run_upi_india_phase(
        client,
        token=token,
        step=step,
        steps=steps,
        cs_id=cs_id,
        processor_entity=processor_entity,
        stripe_pk=stripe_pk,
        customer_session_secret=customer_session_secret,
        checkout_client_secret=checkout_client_secret,
        checkout_ui_mode=checkout_ui_mode,
        referer=referer,
        india_base=india_base,
        billing=billing,
        promo_payload=checkout,
        setup_proxies=setup_proxies,
        use_promo=use_promo,
        promo_campaign=promo_campaign,
        cancelled=cancelled,
        force_payment_method=force_payment_method,
        force_zero=force_zero,
        wait_paid=wait_paid,
        paid_timeout=paid_timeout,
        upi_rail=upi_rail,
        upi_setup_v2=upi_setup_v2,
        require_server_upi_mandate=require_server_upi_mandate,
        requested_upi_mandate=requested_upi_mandate,
        upi_har_context=upi_har_context,
    )


def _upi_billing_address_payload(billing: dict[str, str]) -> dict[str, str]:
    address = {
        'line1': billing['line1'],
        'city': billing['city'],
        'state': billing['state'],
        'country': 'IN',
        'postal_code': billing['postal_code'],
    }
    if billing.get('line2'):
        address['line2'] = billing['line2']
    return address


def _submit_upi_taxes(
    client: UpiHttpClient,
    *,
    token: str,
    cs_id: str,
    processor_entity: str,
    billing: dict[str, str],
    referer: str,
    proxy: str,
) -> dict[str, Any]:
    body = {
        'checkout_session_id': cs_id,
        'checkout_email': billing['email'],
        'billing_country': 'IN',
        'billing_name': billing['name'],
        'currency': 'inr',
        'tax_id': None,
        'processor_entity': processor_entity,
        'billing_address': _upi_billing_address_payload(billing),
    }
    headers = _target_headers(
        _chatgpt_headers(client, token),
        '/backend-api/payments/checkout/taxes',
        referer,
    )
    status, text, _ = client.request(
        'POST',
        'https://chatgpt.com/backend-api/payments/checkout/taxes',
        headers=headers,
        json_body=body,
        proxy=proxy,
    )
    return _json_or_error(status, text, 'chatgpt taxes')


def _submit_upi_billing_snapshot(
    client: UpiHttpClient,
    *,
    token: str,
    billing: dict[str, str],
    referer: str,
    proxy: str,
) -> tuple[int, str]:
    body = {
        'snapshot': {
            'billing_address': {
                'name': billing['name'],
                'address': _upi_billing_address_payload(billing),
            },
        },
    }
    headers = _target_headers(
        _chatgpt_headers(client, token),
        '/backend-api/payments/checkout/snapshot',
        referer,
    )
    status, text, _ = client.request(
        'POST',
        'https://chatgpt.com/backend-api/payments/checkout/snapshot',
        headers=headers,
        json_body=body,
        proxy=proxy,
        timeout=25.0,
    )
    return status, text


def _run_upi_india_phase(client: UpiHttpClient, *, token: str, step: Any, steps: list[dict[str, str]], cs_id: str, processor_entity: str, stripe_pk: str, customer_session_secret: str = '', checkout_client_secret: str = '', checkout_ui_mode: str = 'deferred', referer: str, india_base: str, billing: dict[str, str], promo_payload: dict[str, Any], setup_proxies: list[str] | None = None, use_promo: bool = True, promo_campaign: str | None = None, cancelled: Any = None, on_qr_update: Any = None, force_payment_method: bool = False, force_zero: bool = False, wait_paid: bool = False, paid_timeout: float = 900.0, upi_rail: str = 'upi', upi_setup_v2: bool = False, require_server_upi_mandate: bool = False, requested_upi_mandate: dict[str, Any] | None = None, upi_har_context: bool = False) -> dict[str, Any]:
    _ensure_upi_not_cancelled(cancelled)
    provider_proxy = client.base_proxy or india_base
    checkout_risk = _capture_upi_browser_risk(
        client,
        access_token=token,
        proxy=provider_proxy,
        page_url=referer,
        flow='',
    )
    if not _upi_risk_context_ok(client, checkout_risk) or not checkout_risk.get('stripe_ids'):
        detail = str(checkout_risk.get('error') or '').strip()
        if _upi_protocol_client(client):
            reason = '纯协议模式缺少签名观察证明、Cookie 或 Stripe guid/muid/sid/stripe_js_id'
        else:
            reason = (
                '当前 Firefox 支付页未捕获浏览器 attestation、'
                'signed observation、Cookie 或 Stripe guid/muid/sid/stripe_js_id'
            )
        raise RuntimeError('UPI 风控上下文生成失败：' + reason + (f'; {detail[:240]}' if detail else ''))
    step(
        '支付页观察证明',
        'ok',
        (
            'macOS 纯协议上下文已就绪：部署证明='
            + ('已获取' if client.attestation else '未获取（沿用无证明直连）')
            + '，四个 Stripe ID 已生成'
        ) if _upi_protocol_client(client)
        else '已从当前 Firefox Checkout 页面捕获浏览器 attestation、signed observation、Cookie 和四个 Stripe ID',
    )
    _warmup_india_client(client, token, provider_proxy)
    try:
        client.request('POST', 'https://chatgpt.com/backend-api/sentinel/ping', headers=_target_headers(_chatgpt_headers(client, token), '/backend-api/sentinel/ping', referer), json_body={}, proxy=provider_proxy, timeout=25.0)
    except Exception:
        pass
    step('支付页预热', 'ok', '定价、订阅状态和 Sentinel 连通性检查完成')
    _human_pause('med')

    # The migrated source UPI CS chain is:
    # payment_pages/init -> elements/sessions -> tax_region updates ->
    # checkout/snapshot -> payment_pages/confirm -> approve -> poll.
    # OAICS keeps its own ChatGPT taxes -> confirmation_tokens sequence below.
    stripe_ids = _locked_stripe_request_ids(client)
    stripe_js_id = stripe_ids['stripe_js_id']
    is_custom_checkout = str(checkout_ui_mode or '').strip().lower() == 'custom'
    init_data: dict[str, Any] = {}
    # Match MoMo's OAICS sequence: refresh the authenticated Checkout state
    # once after an oaics_ session is created, then use that state for the
    # customer secret, amount and available payment methods.
    oaics_state: dict[str, Any] = promo_payload if isinstance(promo_payload, dict) else {}
    if not is_custom_checkout:
        try:
            refreshed_state = _fetch_upi_oaics_state(
                client,
                token,
                cs_id=cs_id,
                processor_entity=processor_entity,
                proxy=provider_proxy,
            )
            if refreshed_state:
                oaics_state = {**oaics_state, **refreshed_state}
                refreshed_secret = _checkout_prerequisite_string(
                    refreshed_state,
                    json.dumps(refreshed_state, ensure_ascii=False),
                    ('customer_session_client_secret', 'customer_session_secret'),
                )
                if refreshed_secret:
                    customer_session_secret = refreshed_secret
                step(
                    'OAICS状态刷新',
                    'ok',
                    '已按 MoMo 逻辑刷新 Checkout state；'
                    f'payment_method_types={",".join(_payment_method_types(oaics_state) or ["none"])}',
                )
        except Exception as exc:
            # The create response remains a valid fallback for older accounts;
            # do not turn a non-essential refresh failure into a local abort.
            step('OAICS状态刷新', 'warn', f'刷新失败，将沿用建单响应：{type(exc).__name__}')
        if not customer_session_secret:
            raise RuntimeError('UPI OAICS payment prerequisites missing: customer_session_client_secret')
        state_methods = _payment_method_types(oaics_state)
        if not state_methods:
            raise RuntimeError('UPI OAICS Checkout payment method list is empty')
        if 'upi' not in state_methods:
            if not force_payment_method:
                raise RuntimeError(
                    'UPI OAICS Checkout does not offer upi: '
                    + ','.join(state_methods)
                )
            step(
                '选择UPI支付通道',
                'warn',
                f'OAICS state 未返回 upi（={",".join(state_methods)}），已按 --force-payment-method 继续',
            )
    amount = _amount_from_payload(oaics_state)
    if is_custom_checkout:
        init_data = _stripe_custom_checkout_init(
            client,
            cs_id=cs_id,
            stripe_pk=stripe_pk,
            stripe_js_id=stripe_js_id,
            proxy=provider_proxy,
            use_promo=use_promo,
        )
        init_amount = _amount_from_payload(init_data)
        if init_amount is not None:
            amount = init_amount
        if use_promo and init_amount is not None:
            _require_zero_due(amount, 'Stripe初始化', force=force_zero, on_warn=_zero_warn(step, '0元金额校验'))
        elif not use_promo and amount is None:
            raise RuntimeError(f'UPI original-price amount unavailable or non-positive: {amount!r}')
        step('初始化Stripe支付页', 'ok', f'金额={amount}；配置编号={str(init_data.get("config_id") or "")[:12]}…')

    payment_method_configuration_id = _nested_string(
        promo_payload,
        ('payment_method_configuration_id', 'payment_method_configuration_id_hint', 'pmc_id'),
    ) if not use_promo else ''
    # Checkout/Elements may omit a total temporarily. Keep going until Stripe
    # init/tax/update returns an authoritative amount; the strict zero gate is
    # enforced immediately before confirmation.
    elements_amount = amount if amount is not None else 0
    elements = _elements_session(
        client,
        customer_session_secret=customer_session_secret,
        stripe_pk=stripe_pk,
        amount=elements_amount,
        stripe_js_id=stripe_js_id,
        proxy=provider_proxy,
        checkout_session_id=cs_id,
        checkout_ui_mode=checkout_ui_mode,
        use_promo=use_promo,
        payment_method_configuration_id=payment_method_configuration_id,
        # OAICS exposes the authoritative method list in the Checkout state;
        # custom/payment_pages keeps the HAR-proven card/link/upi ordering.
        payment_method_types=(
            _payment_method_types(oaics_state) if not is_custom_checkout else None
        ),
    )
    if str(elements.get('session_id') or '').strip() == '':
        raise RuntimeError('UPI Stripe Elements session failed: session_id missing')
    if not is_custom_checkout:
        step(
            'OAICS参数协商',
            'ok',
            '已发送 Stripe 基础版本、locale、时区和服务端支付方式'
            + f'；methods={",".join(_payment_method_types(elements) or _payment_method_types(promo_payload) or ["card", "upi"])}',
        )
    _ensure_upi_offered(
            elements, 'Stripe Custom Checkout' if is_custom_checkout else 'Stripe OAICS Elements',
            force=force_payment_method,
            on_warn=(lambda phase, methods: step(
                '选择UPI支付通道', 'warn',
                f'Stripe 支付组件未返回 upi（={",".join(methods) or "none"}），已按 --force-payment-method 强制提交 upi'
            )) if force_payment_method else None,
        )
    step('初始化UPI支付组件', 'ok', f'支付界面模式={_upi_status_cn(checkout_ui_mode)}；金额={amount}；组件会话={str(elements.get("session_id"))[:16]}…')
    _human_pause('short')

    if is_custom_checkout:
        country_state = _stripe_custom_checkout_update(
            client,
            cs_id=cs_id,
            stripe_pk=stripe_pk,
            stripe_js_id=stripe_js_id,
            elements=elements,
            billing=billing,
            proxy=provider_proxy,
            address_phase='country',
        )
        country_amount = _amount_from_payload(country_state)
        if country_amount is not None:
            amount = country_amount
        if use_promo and country_amount is not None:
            _require_zero_due(country_amount, 'Stripe国家税区更新', force=force_zero, on_warn=_zero_warn(step, '0元金额校验'))
        step('更新Stripe税费国家', 'ok', '账单国家已设置为印度')
        if require_server_upi_mandate:
            returned_options = init_data.get('payment_method_options') if isinstance(init_data, dict) else {}
            returned_upi = returned_options.get('upi') if isinstance(returned_options, dict) else {}
            returned_mandate = returned_upi.get('mandate_options') if isinstance(returned_upi, dict) else {}
            requested = requested_upi_mandate if isinstance(requested_upi_mandate, dict) else {}
            matches = bool(requested and returned_mandate and all(
                str(requested.get(key) or '') == str(returned_mandate.get(key) or '')
                for key in ('amount', 'amount_type', 'description', 'end_date')
            ))
            step(
                '校验UPI授权参数',
                'ok' if matches else 'warn',
                '服务端 mandate_options=' + ('matched' if matches else 'mismatch/missing')
                + '；继续沿用旧版 inline confirm',
            )

    # The source implementation only posts ChatGPT /taxes on the OAICS rail.
    # Stripe Custom Checkout obtains the authoritative amount from the
    # payment_pages tax_region response instead.
    if not is_custom_checkout:
        taxes = _submit_upi_taxes(
            client,
            token=token,
            cs_id=cs_id,
            processor_entity=processor_entity,
            billing=billing,
            referer=referer,
            proxy=provider_proxy,
        )
        # Taxes is the latest authoritative total. Missing or malformed
        # amounts must not silently inherit a zero from the create response.
        amount = _amount_from_payload(taxes)
    if use_promo:
        if amount is None or int(amount) != 0:
            if force_zero:
                step('0元金额校验', 'warn', f'当前应付金额={amount}，已按强压 0 元继续提交（实际金额以服务端为准）')
            else:
                step('0元金额校验', 'warn', f'当前应付金额={amount}，本次任务已终止')
        _require_zero_due(amount, '优惠后', force=force_zero, on_warn=_zero_warn(step, '0元金额校验'))
    elif amount is None or int(amount) <= 0:
        raise RuntimeError(f'UPI original-price amount unavailable or non-positive: {amount!r}')
    step('提交印度账单与税费', 'ok', f'账单国家=印度；最终应付金额={amount if amount is not None else "待Stripe确认"}')
    _human_pause('short')

    # 账单快照(对齐 GoPay/GCash 链路文档 Step 4 与优惠 HAR #235):
    # OpenAI 侧保存账单快照,confirm/approve 会话更完整。失败仅 warn 不阻断。
    try:
        st_snap, snap_text = _submit_upi_billing_snapshot(
            client,
            token=token,
            billing=billing,
            referer=referer,
            proxy=provider_proxy,
        )
        if st_snap in (200, 204):
            step('保存账单快照', 'ok', '账单地址快照提交成功')
        else:
            step('保存账单快照', 'warn', f'账单快照返回 HTTP {st_snap}，不影响后续流程')
    except Exception:
        step('保存账单快照', 'warn', '账单快照提交失败，不影响后续流程')
    _human_pause('short')

    if is_custom_checkout:
        # The legacy CS rail confirms directly after the tax_region sequence
        # and the ChatGPT snapshot. It does not perform the newer
        # payment-page GET/full-address update pair.
        if use_promo:
            _require_zero_due(amount, 'confirm前Stripe最终金额', force=force_zero)
        legacy_result = _run_upi_custom_checkout(
            client,
            token=token,
            step=step,
            steps=steps,
            cs_id=cs_id,
            processor_entity=processor_entity,
            stripe_pk=stripe_pk,
            checkout_client_secret=checkout_client_secret,
            stripe_js_id=stripe_js_id,
            elements=elements,
            init_data=init_data,
            checkout_config_id=str(
                (country_state.get('config_id') if isinstance(country_state, dict) else '')
                or init_data.get('config_id')
                or ''
            ),
            amount=amount,
            billing=billing,
            proxy=provider_proxy,
            setup_proxies=setup_proxies,
            referer=referer,
            cancelled=cancelled,
            on_qr_update=on_qr_update,
            use_promo=use_promo,
            promo_campaign=promo_campaign,
            force_zero=force_zero,
            wait_paid=wait_paid,
            paid_timeout=paid_timeout,
        )
        legacy_result.update({
            'upi_rail': _upi_rail_str(upi_rail),
            'upi_setup_v2': bool(upi_setup_v2),
            'upi_har_context': bool(upi_har_context),
            'flow_variant': (
                'macos_chrome_har_context_v5'
                if _upi_is_protocol_rail(upi_rail)
                else 'legacy_upi_cs_inline_confirm'
            ),
        })
        return legacy_result

    if use_promo:
        # OAICS may omit totals in Checkout and Elements responses. The gate
        # belongs here, immediately before confirmation token/checkout confirm.
        _require_zero_due(amount, 'confirm前Stripe最终金额', force=False)
    _upi_refresh_browser_risk(
        client,
        access_token=token,
        proxy=provider_proxy,
        page_url=referer,
        flow='',
        max_age_s=UPI_OBSERVATION_MAX_AGE_S,
        step=step,
        stage='OAICS confirmation token前风控复核',
    )
    started_ms = int(time.monotonic() * 1000)
    confirmation = _confirmation_token(
        client,
        checkout_session_id=cs_id,
        stripe_pk=stripe_pk,
        billing=billing,
        stripe_js_id=stripe_js_id,
        elements=elements,
        payment_method_types=(
            _payment_method_types(oaics_state)
            or _payment_method_types(promo_payload)
            or _payment_method_types(elements)
        ),
        proxy=provider_proxy,
        time_on_page_ms=max(5000, int(time.monotonic() * 1000) - started_ms + 5000),
    )
    confirm_token = _first_string(confirmation, ['id'])
    if not confirm_token.startswith('ctoken_'):
        raise RuntimeError('HAR UPI confirmation_tokens did not return ctoken')
    step('生成支付确认令牌', 'ok', f'确认令牌已生成（{confirm_token[:16]}…）')
    _human_pause('short')

    _sentinel_ping(client, token, provider_proxy, referer)
    chat_confirm = _checkout_confirm_with_token(client, token=token, cs_id=cs_id, processor_entity=processor_entity, confirm_token=confirm_token, proxy=provider_proxy, step=step)
    intent_type, intent_id, client_secret = _checkout_intent(chat_confirm)
    step('确认ChatGPT订单', 'ok', f'支付对象已生成（{intent_type}）')
    _human_pause('long')

    _upi_refresh_browser_risk(
        client,
        access_token=token,
        proxy=provider_proxy,
        page_url=referer,
        flow='',
        max_age_s=UPI_OBSERVATION_MAX_AGE_S,
        step=step,
        stage='Stripe intent confirm前风控复核',
    )
    confirm_kwargs = {
        'client_secret': client_secret,
        'confirmation_token': confirm_token,
        'stripe_pk': stripe_pk,
        'stripe_js_id': stripe_js_id,
        'return_url': _first_string(chat_confirm, ['confirm_return_url']) or _har_return_url(cs_id, processor_entity),
        'proxy': provider_proxy,
    }
    step('确认UPI支付意图', 'run', '正在向 Stripe 确认 UPI 支付意图')
    if intent_type == 'setup_intent':
        pi_confirm = _stripe_setup_intent_confirm(client, setup_intent_id=intent_id, **confirm_kwargs)
    else:
        pi_confirm = _stripe_payment_intent_confirm(client, payment_intent_id=intent_id, **confirm_kwargs)
    _raise_for_upi_intent_failure(pi_confirm)
    # UPI 支付意图 confirm 后即进入 requires_action 并携带二维码
    # （HAR 印度UPI.har #273）；立即推给前端展示，再轮询到账。
    pi_qr = _extract_upi_qr(pi_confirm)
    if pi_qr:
        _emit_upi_qr(pi_qr, on_qr_update, step, client)
    step('确认UPI支付意图', 'ok', f'支付状态={_upi_status_cn(pi_confirm.get("status") or "requires_action")}')
    payment_material = pi_confirm
    if not _upi_copyable_link(payment_material, pi_qr):
        step('查询UPI支付意图状态', 'run', '正在查询 Stripe 支付意图状态')
        poll_kwargs = {
            'client_secret': client_secret, 'stripe_pk': stripe_pk, 'proxy': provider_proxy,
            'cancelled': cancelled, 'max_attempts': UPI_PAYMENT_POLL_ATTEMPTS,
            'on_qr_update': on_qr_update, 'step': step,
        }
        if intent_type == 'setup_intent':
            payment_material = _poll_stripe_setup_intent(client, setup_intent_id=intent_id, **poll_kwargs)
        else:
            payment_material = _poll_stripe_payment_intent(client, payment_intent_id=intent_id, steps=steps, **poll_kwargs)
    _raise_for_upi_intent_failure(payment_material)
    payment_qr = _extract_upi_qr(payment_material) or pi_qr or {}
    # Preserve the source extractor's fallback to the legacy UPI deep link;
    # never expose the qr.stripe.com image URL as the payment link.
    long_url = _upi_copyable_link(payment_material, payment_qr)
    final_state = str(payment_material.get('status') or '').strip()
    if not long_url:
        state = final_state
        error = payment_material.get('last_payment_error') or payment_material.get('error') or ''
        raise RuntimeError(f'UPI HAR payment intent 未生成可复制 UPI 链接：status={state or "unknown"}; error={str(error)[:240]}')
    zero_verified = (not use_promo) or int(amount) == 0
    link_accepted = bool(force_zero or zero_verified)
    detail = ('UPI 支付链接与二维码已生成' if zero_verified else
              ('UPI 支付链接与二维码已生成（--force-zero，最终金额以服务端为准）' if force_zero else
               f'支付链接已返回，但优惠未生效（应付金额={amount}），本轮不作为成功结果'))
    step('生成支付链接和二维码', 'ok' if link_accepted else 'warn', detail)
    return {
        'success': True,
        'ok': True,
        'long_url': long_url,
        'url': long_url,
        'provider_redirect_url': long_url,
        'cs_id': cs_id,
        'checkout_session_id': cs_id,
        'billing_country': 'IN',
        'currency': 'INR',
        'checkout_currency': 'INR',
        'amount': amount,
        'checkout_amount': amount,
        'promo_applied': (amount == 0) if use_promo else None,
        'promo_requested': bool(use_promo),
        'promo_campaign_used': str(promo_campaign or UPI_PROMO_COUPON) if use_promo else '',
        'payment_method_types': ['upi'],
        'confirmation_token_id': confirm_token,
        'payment_object_type': intent_type,
        'payment_intent_id': intent_id if intent_type == 'payment_intent' else '',
        'setup_intent_id': intent_id if intent_type == 'setup_intent' else '',
        'elements_session_id': str(elements.get('session_id') or ''),
        'payment_intent_status': final_state if intent_type == 'payment_intent' else '',
        'setup_intent_status': final_state if intent_type == 'setup_intent' else '',
        'qr_url': str(payment_qr.get('qr_url') or ''),
        'hosted_instructions_url': str(payment_qr.get('hosted_instructions_url') or ''),
        'upi_deep_link': str(payment_qr.get('upi_deep_link') or ''),
        'link_type': 'upi_deep_link' if str(payment_qr.get('upi_deep_link') or '').startswith('upi://') and not payment_qr.get('hosted_instructions_url') else 'upi_qr',
        'qr_image_url_png': str(payment_qr.get('image_url_png') or ''),
        'qr_image_url_svg': str(payment_qr.get('image_url_svg') or ''),
        'expires_at': payment_qr.get('expires_at') or 0,
        'paid': final_state == 'succeeded',
        'payment_status': final_state,
        'upi_rail': _upi_rail_str(upi_rail),
        'upi_setup_v2': bool(upi_setup_v2),
        'upi_har_context': bool(upi_har_context),
        'flow_variant': (
            'macos_chrome_har_context_v5'
            if _upi_is_protocol_rail(upi_rail)
            else 'legacy_upi_oaics_confirmation_token'
        ),
    }
def _is_rebuild_retryable(exc: Exception) -> bool:
    detail = str(exc).lower()
    if 'blocked by risk control' in detail or 'result=blocked' in detail:
        return False
    if 'network request failed' in detail or 'proxy connect' in detail or 'curl:' in detail:
        return False
    if 'amount policy' in detail or 'upi promo not applied' in detail or 'upi promo amount unavailable' in detail:
        return True
    if 'redirect not ready' in detail or 'redirect url resolution' in detail:
        return True
    return any((code in detail for code in ('502', '503', '504')))
