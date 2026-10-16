# -*- coding: utf-8 -*-
"""GoPay 印尼账单：坐标反查与地址来源优先级。

全部离线：所有网络调用都被替换或阻断，测试不会请求真实 Nominatim / ip-api。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gopay_custom_flow as flow  # noqa: E402
import id_billing as ib  # noqa: E402


JAKARTA_REVERSE = {
    'line1': 'Jl. Jend. Sudirman 20',
    'city': 'Jakarta',
    'state': 'DKI Jakarta',
    'postal_code': '12190',
    'country': 'ID',
    'source': 'openstreetmap_nominatim_reverse',
}


class OfflineTestCase(unittest.TestCase):
    """阻断 socket，任何漏网的网络调用都会让测试失败而不是真的发请求。"""

    def setUp(self):
        for target in ('socket.create_connection', 'socket.socket.connect'):
            blocker = patch(target, side_effect=AssertionError('GoPay billing tests must stay offline'))
            blocker.start()
            self.addCleanup(blocker.stop)


class ReverseAddressTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.enterContext(
            patch.object(ib, '_REVERSE_CACHE_PATH', os.path.join(directory, 'reverse.json'))
        )
        self.enterContext(patch.object(ib, '_LAST_NOMINATIM_AT', 0.0))
        self.enterContext(patch.object(ib.time, 'sleep', return_value=None))

    @staticmethod
    def nominatim_payload():
        return {
            'display_name': '20, Jl. Jend. Sudirman, Jakarta, Indonesia',
            'address': {
                'country_code': 'id',
                'road': 'Jl. Jend. Sudirman',
                'house_number': '20',
                'city': 'Jakarta',
                'state': 'DKI Jakarta',
                'postcode': '12190',
            },
        }

    def test_valid_coordinates_map_to_an_address_without_a_name(self):
        with patch.object(ib, '_nominatim_reverse_once', return_value=self.nominatim_payload()) as call:
            resolved = ib.resolve_reverse_address(-6.2088, 106.8456, 'ID')
        self.assertEqual(resolved['line1'], 'Jl. Jend. Sudirman 20')
        self.assertEqual(resolved['city'], 'Jakarta')
        self.assertEqual(resolved['postal_code'], '12190')
        self.assertEqual(resolved['source'], 'openstreetmap_nominatim_reverse')
        # 地图的 name 是商户/地标名，绝不能作为账单姓名来源。
        self.assertNotIn('name', resolved)
        self.assertEqual(call.call_count, 1)

    def test_second_lookup_is_served_from_cache(self):
        with patch.object(ib, '_nominatim_reverse_once', return_value=self.nominatim_payload()) as call:
            first = ib.resolve_reverse_address(-6.2088, 106.8456, 'ID')
            second = ib.resolve_reverse_address(-6.2088, 106.8456, 'ID')
        self.assertEqual(first, second)
        self.assertEqual(call.call_count, 1)

    def test_foreign_country_result_is_rejected(self):
        payload = {'address': {'country_code': 'sg', 'road': 'Orchard Road', 'city': 'Singapore'}}
        with patch.object(ib, '_nominatim_reverse_once', return_value=payload):
            self.assertIsNone(ib.resolve_reverse_address(-6.2088, 106.8456, 'ID'))

    def test_missing_street_or_city_is_not_usable(self):
        for payload in (
            {'address': {'country_code': 'id', 'city': 'Jakarta'}},
            {'address': {'country_code': 'id', 'road': 'Jl. Sudirman'}},
            {'address': {'country_code': 'id'}},
        ):
            with self.subTest(payload=payload):
                with patch.object(ib, '_nominatim_reverse_once', return_value=payload):
                    self.assertIsNone(ib.resolve_reverse_address(-6.2, 106.8, 'ID'))

    def test_invalid_coordinates_are_rejected_without_network(self):
        for lat, lon in (('', ''), ('x', '1'), (91.0, 0.0), (0.0, 181.0), (None, 5.0)):
            with self.subTest(latitude=lat, longitude=lon):
                with patch.object(ib, '_nominatim_reverse_once') as call:
                    self.assertIsNone(ib.resolve_reverse_address(lat, lon, 'ID'))
                call.assert_not_called()

    def test_lookup_failure_returns_none_instead_of_raising(self):
        for effect in (RuntimeError('proxy unreachable'), ValueError('bad json')):
            with self.subTest(effect=type(effect).__name__):
                with patch.object(ib, '_nominatim_reverse_once', side_effect=effect):
                    self.assertIsNone(ib.resolve_reverse_address(-6.2, 106.8, 'ID'))


class IdBillingAddressSourceTests(OfflineTestCase):
    """地址来源优先级：环境注入 > 坐标反查 > 内置街道表。"""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'MIN_GOPAY_BILLING_ADDRESS': ''}))

    def test_reverse_address_wins_over_the_builtin_street_table(self):
        billing = ib.generate_id_billing(
            'acct@example.test', country='ID', currency='IDR',
            geo_city='Surabaya', reverse_address=JAKARTA_REVERSE,
        )
        self.assertEqual(billing['_source'], 'openstreetmap_nominatim_reverse')
        self.assertEqual(billing['address']['city'], 'Jakarta')
        self.assertEqual(billing['address']['line1'], 'Jl. Jend. Sudirman 20')
        # geo_city=Surabaya 不得覆盖反查结果
        self.assertNotEqual(billing['address']['city'], 'Surabaya')

    def test_nested_address_shape_is_accepted(self):
        billing = ib.generate_id_billing('acct@example.test', reverse_address={'address': JAKARTA_REVERSE})
        self.assertEqual(billing['_source'], 'openstreetmap_nominatim_reverse')

    def test_billing_name_always_comes_from_the_local_name_pool(self):
        billing = ib.generate_id_billing('acct@example.test', reverse_address=JAKARTA_REVERSE)
        first, last = billing['name'].split(' ', 1)
        self.assertIn(first, ib._FIRST_NAMES)
        self.assertIn(last, ib._LAST_NAMES)
        # 反查的 _place_name 不参与姓名
        self.assertNotIn(billing['name'], (billing.get('_place_name') or '',))

    def test_unusable_reverse_address_falls_back_to_the_builtin_table(self):
        cases = {
            'short postal': {**JAKARTA_REVERSE, 'postal_code': '1219'},
            'empty postal': {**JAKARTA_REVERSE, 'postal_code': ''},
            'wrong country': {**JAKARTA_REVERSE, 'country': 'SG'},
            'missing street': {**JAKARTA_REVERSE, 'line1': ''},
            'missing city': {**JAKARTA_REVERSE, 'city': ''},
            'missing state': {**JAKARTA_REVERSE, 'state': ''},
        }
        for label, reverse in cases.items():
            with self.subTest(case=label):
                billing = ib.generate_id_billing('acct@example.test', reverse_address=reverse)
                self.assertEqual(billing['_source'], 'builtin_public_id_street')
                self.assertTrue(ib._is_valid_reverse_address(billing['address'], 'ID'), label)

    def test_no_reverse_address_keeps_the_existing_behaviour(self):
        billing = ib.generate_id_billing('acct@example.test', geo_city='Bandung')
        self.assertEqual(billing['_source'], 'builtin_public_id_street')
        self.assertEqual(billing['address']['city'], 'Bandung')
        self.assertTrue(billing['_geo_city_matched'])

    def test_env_injection_still_outranks_the_reverse_address(self):
        injected = {'name': 'Injected Person', 'address': {
            'line1': 'Jl. Test 1', 'city': 'Bogor', 'postal_code': '16143', 'country': 'ID'}}
        with patch.dict(os.environ, {'MIN_GOPAY_BILLING_ADDRESS': json.dumps(injected)}):
            billing = ib.generate_id_billing('acct@example.test', reverse_address=JAKARTA_REVERSE)
        self.assertEqual(billing['_source'], 'env_injected')
        self.assertEqual(billing['name'], 'Injected Person')
        self.assertEqual(billing['address']['city'], 'Bogor')


class ReverseBillingToggleTests(OfflineTestCase):
    """开关与出口国家判断：不该反查时必须一次地图请求都不发。"""

    def test_toggle_defaults_to_enabled(self):
        for value, expected in (
            ('', True), ('1', True), ('true', True), ('yes', True), ('on', True),
            ('0', False), ('false', False), ('no', False), ('off', False),
        ):
            with self.subTest(value=value):
                with patch.dict(os.environ, {'GOPAY_BILLING_REVERSE': value}):
                    self.assertEqual(flow._gopay_billing_reverse_enabled(), expected)

    def test_disabled_toggle_never_probes(self):
        fingerprint = {'proxy_country': 'ID', 'proxy_latitude': '-6.2', 'proxy_longitude': '106.8'}
        with patch.dict(os.environ, {'GOPAY_BILLING_REVERSE': '0'}), patch.object(
            ib, 'resolve_reverse_address'
        ) as lookup:
            self.assertEqual(flow._reverse_gopay_billing(fingerprint, 'ID'), {})
        lookup.assert_not_called()

    def test_missing_coordinates_never_probes(self):
        for fingerprint in (
            {},
            {'proxy_country': 'ID'},
            {'proxy_country': 'ID', 'proxy_latitude': '-6.2'},
            {'proxy_country': 'ID', 'proxy_longitude': '106.8'},
        ):
            with self.subTest(fingerprint=fingerprint):
                with patch.object(ib, 'resolve_reverse_address') as lookup:
                    self.assertEqual(flow._reverse_gopay_billing(fingerprint, 'ID'), {})
                lookup.assert_not_called()

    def test_proxy_country_must_match_the_billing_country(self):
        fingerprint = {'proxy_country': 'SG', 'proxy_latitude': '1.3', 'proxy_longitude': '103.8'}
        with patch.object(ib, 'resolve_reverse_address') as lookup:
            self.assertEqual(flow._reverse_gopay_billing(fingerprint, 'ID'), {})
        lookup.assert_not_called()

    def test_matching_country_probes_and_returns_the_result(self):
        fingerprint = {'proxy_country': 'ID', 'proxy_latitude': '-6.2088', 'proxy_longitude': '106.8456'}
        with patch.object(ib, 'resolve_reverse_address', return_value=JAKARTA_REVERSE) as lookup:
            resolved = flow._reverse_gopay_billing(fingerprint, 'ID')
        self.assertEqual(resolved['city'], 'Jakarta')
        lookup.assert_called_once_with('-6.2088', '106.8456', 'ID')

    def test_lookup_exception_falls_back_to_the_builtin_table(self):
        fingerprint = {'proxy_country': 'ID', 'proxy_latitude': '-6.2', 'proxy_longitude': '106.8'}
        with patch.object(ib, 'resolve_reverse_address', side_effect=RuntimeError('boom')):
            self.assertEqual(flow._reverse_gopay_billing(fingerprint, 'ID'), {})


class ExitGeoProbeTests(OfflineTestCase):
    """probe_exit_geo 的字段映射与失败降级；probe_exit_city 保持兼容。"""

    @staticmethod
    def response(payload):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = payload
        return response

    def test_ip_api_success_maps_country_city_and_coordinates(self):
        payload = {'status': 'success', 'countryCode': 'ID', 'regionName': 'DKI Jakarta',
                   'city': 'Jakarta', 'zip': '12190', 'lat': -6.2088, 'lon': 106.8456}
        with patch.object(ib, '_GEO_PROBE_IMPERSONATE', 'chrome136'), patch(
            'curl_cffi.requests.Session'
        ) as session_cls:
            session_cls.return_value.get.return_value = self.response(payload)
            geo = ib.probe_exit_geo('http://proxy.invalid:8080')
        self.assertEqual(geo['country'], 'ID')
        self.assertEqual(geo['city'], 'Jakarta')
        self.assertEqual(geo['latitude'], '-6.2088')
        self.assertEqual(geo['longitude'], '106.8456')

    def test_probe_exit_city_still_returns_just_the_city(self):
        payload = {'status': 'success', 'countryCode': 'ID', 'city': 'Bandung', 'lat': -6.9, 'lon': 107.6}
        with patch('curl_cffi.requests.Session') as session_cls:
            session_cls.return_value.get.return_value = self.response(payload)
            self.assertEqual(ib.probe_exit_city('http://proxy.invalid:8080'), 'Bandung')

    def test_empty_proxy_returns_nothing_without_a_request(self):
        with patch('curl_cffi.requests.Session') as session_cls:
            self.assertEqual(ib.probe_exit_geo(''), {})
            self.assertEqual(ib.probe_exit_city(''), '')
        session_cls.assert_not_called()

    def test_probe_failure_degrades_to_empty(self):
        with patch('curl_cffi.requests.Session') as session_cls:
            session_cls.return_value.get.side_effect = RuntimeError('proxy down')
            self.assertEqual(ib.probe_exit_geo('http://proxy.invalid:8080'), {})


if __name__ == '__main__':
    unittest.main()
