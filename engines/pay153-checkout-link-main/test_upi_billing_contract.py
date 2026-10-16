from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import billing_address_resolver as resolver
import upi_link_service as upi

MUMBAI_ROW = ('Apollo Bunder, Colaba', 'Mumbai', 'MH', '400001')
BENGALURU_ROW = ('1 Residency Road', 'Bengaluru', 'KA', '560025')
KERALA_ROW = ('Marine Drive', 'Kochi', 'KL', '682031')


def caller_billing() -> dict:
    return {
        'name': 'Billing Test',
        'email': 'billing@example.test',
        'address': {
            'line1': 'Customer supplied street',
            'line2': 'Unit 12',
            'city': 'Mumbai',
            'state': 'MH',
            'postal_code': '400026',
            'country': 'IN',
        },
    }


def address_row(result: dict) -> tuple[str, str, str, str]:
    address = result['address']
    return tuple(address[key] for key in ('line1', 'city', 'state', 'postal_code'))


class OfflineBillingTestCase(unittest.TestCase):
    def setUp(self):
        for target in ('socket.create_connection', 'socket.socket.connect'):
            blocker = patch(target, side_effect=AssertionError('Billing must remain offline'))
            blocker.start()
            self.addCleanup(blocker.stop)


class IndianAddressValidationTests(OfflineBillingTestCase):
    def test_city_state_pin_must_agree(self):
        cases = (
            ('Mumbai', 'MH', '400026', True),
            ('Mumbai', 'Maharashtra', '400026', True),
            ('Mumbai', 'KA', '110001', False),
            ('Mumbai', 'MH', '110001', False),
            ('Mumbai', 'ZZ', '999999', False),
            ('Unknown City', 'MH', '400026', False),
            ('Bengaluru', 'KA', '560038', True),
            ('Bengaluru', 'MH', '560038', False),
        )
        for city, state, postal, expected in cases:
            with self.subTest(city=city, state=state, postal=postal):
                self.assertEqual(
                    upi._is_complete_indian_address('Customer street', city, state, postal),
                    expected,
                )

    def test_country_must_be_india(self):
        for country, expected in (('IN', True), ('India', True), ('in', True), ('US', False)):
            with self.subTest(country=country):
                self.assertEqual(
                    upi._is_complete_indian_address('Customer street', 'Mumbai', 'MH', '400026', country),
                    expected,
                )

    def test_common_city_aliases_are_validated_against_the_same_region(self):
        for city, state, postal in (
            ('Bombay', 'MH', '400026'),
            ('Bangalore', 'Karnataka', '560001'),
        ):
            with self.subTest(city=city):
                self.assertTrue(upi._is_complete_indian_address('Customer street', city, state, postal))
        # 池外地区（Gurgaon/HR）不参与校验，即使本地写法一致也应拒绝
        self.assertFalse(upi._is_complete_indian_address('Customer street', 'Gurgaon', 'HR', '122001'))

    def test_missing_street_and_malformed_pin_are_rejected(self):
        for line1, postal in (
            ('', '400026'), ('   ', '400026'), ('Street', ''),
            ('Street', '40002'), ('Street', '4000267'), ('Street', '40002A'),
            ('Street', '000000'), ('Street', '４０００２６'),
        ):
            with self.subTest(line1=line1, postal=postal):
                self.assertFalse(upi._is_complete_indian_address(line1, 'Mumbai', 'MH', postal))


class BillingPoolTests(OfflineBillingTestCase):
    def test_shared_pool_does_not_increase_duplicate_address_weight(self):
        with patch.object(resolver, 'resolve_public_address', side_effect=AssertionError('Use local rows')):
            rows = upi._upi_billing_address_rows()
        self.assertEqual(len(rows), len(set(rows)))
        self.assertEqual(rows.count(MUMBAI_ROW), 1)
        self.assertIn(BENGALURU_ROW, rows)
        for row in rows:
            with self.subTest(row=row):
                self.assertTrue(upi._is_complete_indian_address(*row))

    def test_malformed_rows_are_excluded_and_resolver_rows_are_merged(self):
        supplemental = {
            'IN': [
                dict(zip(('line1', 'city', 'state', 'postal_code'), MUMBAI_ROW)),
                dict(zip(('line1', 'city', 'state', 'postal_code'), BENGALURU_ROW)),
                {'line1': 'Invalid state', 'city': 'Mumbai', 'state': 'ZZ', 'postal_code': '400001'},
            ],
        }
        primary = (
            MUMBAI_ROW, MUMBAI_ROW,
            ('Bad PIN', 'Mumbai', 'MH', '40000'),
            ('Zero PIN', 'Mumbai', 'MH', '000000'),
            ('No state', 'Mumbai', 'ZZ', '400001'),
            ('', 'Mumbai', 'MH', '400001'),
            ('Missing fields', 'Mumbai'),
        )
        with patch.object(upi, 'UPI_BILLING_ADDRESS_POOL', primary), patch.object(
            resolver, '_BUILTIN_PUBLIC_ADDRESSES', supplemental
        ), patch.object(upi, '_external_billing_address_rows', return_value=[]):
            self.assertCountEqual(upi._upi_billing_address_rows(), [MUMBAI_ROW, BENGALURU_ROW])

    def test_rows_outside_the_configured_states_are_dropped(self):
        supplemental = {
            'IN': [
                dict(zip(('line1', 'city', 'state', 'postal_code'), KERALA_ROW)),
                dict(zip(('line1', 'city', 'state', 'postal_code'), BENGALURU_ROW)),
            ],
        }
        with patch.object(upi, 'UPI_BILLING_ADDRESS_POOL', (MUMBAI_ROW, KERALA_ROW)), patch.object(
            resolver, '_BUILTIN_PUBLIC_ADDRESSES', supplemental
        ), patch.object(upi, '_external_billing_address_rows', return_value=[]):
            rows = upi._upi_billing_address_rows()
        self.assertCountEqual(rows, [MUMBAI_ROW, BENGALURU_ROW])
        self.assertNotIn(KERALA_ROW, rows)

    def test_adjacent_selections_do_not_repeat_when_multiple_rows_are_available(self):
        with patch.object(upi, '_upi_billing_address_rows', return_value=[MUMBAI_ROW, BENGALURU_ROW]):
            selected = [address_row(upi._random_upi_billing_address()) for _ in range(20)]
        self.assertEqual(set(selected), {MUMBAI_ROW, BENGALURU_ROW})
        self.assertTrue(all(first != second for first, second in zip(selected, selected[1:])))

    def test_single_candidate_can_be_reused_without_failure(self):
        with patch.object(upi, '_upi_billing_address_rows', return_value=[MUMBAI_ROW]):
            for _ in range(3):
                self.assertEqual(address_row(upi._random_upi_billing_address()), MUMBAI_ROW)

    def test_city_preference_accepts_alias_and_unknown_city_falls_back(self):
        with patch.object(upi, '_upi_billing_address_rows', return_value=[MUMBAI_ROW, BENGALURU_ROW]):
            self.assertEqual(address_row(upi._random_upi_billing_address('Bangalore')), BENGALURU_ROW)
            self.assertIn(address_row(upi._random_upi_billing_address('Unknown City')), (MUMBAI_ROW, BENGALURU_ROW))


class ExternalBillingPoolFileTests(OfflineBillingTestCase):
    """UPI_BILLING_POOL_FILE is an opt-in operator channel; unset means unchanged."""

    def setUp(self):
        super().setUp()
        upi._reset_upi_billing_row_caches()
        self.addCleanup(upi._reset_upi_billing_row_caches)
        self.enterContext(patch.dict(os.environ, {'UPI_BILLING_POOL_FILE': ''}))

    def write_pool(self, payload, name='pool.json'):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, name)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)
        return path

    def rows_with(self, path):
        os.environ['UPI_BILLING_POOL_FILE'] = path
        upi._reset_upi_billing_row_caches()
        return upi._upi_billing_address_rows()

    def test_no_file_configured_keeps_the_builtin_pool(self):
        self.assertEqual(upi._external_billing_address_rows(), [])
        self.assertEqual(upi._external_billing_pool_file(), '')

    def test_wrapped_object_rows_are_loaded_and_validated(self):
        path = self.write_pool({'addresses': [
            {'line1': '140 Pawasakar Galli Road, Somwar Peth', 'city': 'Karad', 'state': 'MH', 'postal_code': '415100'},
            {'line1': '64 Shivaji Road, Kasba Peth', 'city': 'Pune', 'state': 'MH', 'postal_code': '411001'},
        ]})
        rows = self.rows_with(path)
        self.assertIn(('140 Pawasakar Galli Road, Somwar Peth', 'Karad', 'MH', '415100'), rows)
        # 新城市同时进入抽样池与显式地址校验
        self.assertIn('karad', upi._known_indian_cities())
        self.assertTrue(upi._is_complete_indian_address('X', 'Karad', 'MH', '415100'))

    def test_flat_array_and_jsonl_shapes_are_both_accepted(self):
        flat = self.write_pool([
            {'line1': '64 Shivaji Road, Kasba Peth', 'city': 'Pune', 'state': 'MH', 'postal_code': '411001'},
        ], name='flat.json')
        jsonl = self.write_pool(
            '{"line1": "64 Shivaji Road, Kasba Peth", "city": "Pune", "state": "MH", "postal_code": "411001"}\n',
            name='rows.jsonl',
        )
        expected = ('64 Shivaji Road, Kasba Peth', 'Pune', 'MH', '411001')
        for path in (flat, jsonl):
            with self.subTest(path=path):
                self.assertIn(expected, self.rows_with(path))

    def test_out_of_scope_and_malformed_records_are_dropped(self):
        path = self.write_pool({'addresses': [
            {'line1': 'Marine Drive', 'city': 'Kochi', 'state': 'KL', 'postal_code': '682031'},
            {'line1': 'Bhawani Singh Road', 'city': 'Jaipur', 'state': 'RJ', 'postal_code': '302005'},
            {'line1': '64 Shivaji Road, Kasba Peth', 'city': 'Pune', 'state': 'MH', 'postal_code': '411001'},
            {'line1': 'Bad PIN', 'city': 'Pune', 'state': 'MH', 'postal_code': '41100'},
            {'line1': 'Unknown city', 'city': 'Nowhere', 'state': 'MH', 'postal_code': '411001'},
            {'line1': 'No state', 'city': 'Pune', 'state': 'ZZ', 'postal_code': '411001'},
            {'line1': '', 'city': 'Pune', 'state': 'MH', 'postal_code': '411001'},
            {'line1': 'Not a dict row'},
            'not even an object',
        ]})
        rows = self.rows_with(path)
        self.assertIn(('64 Shivaji Road, Kasba Peth', 'Pune', 'MH', '411001'), rows)
        self.assertNotIn('kochi', upi._known_indian_cities())
        self.assertNotIn('jaipur', upi._known_indian_cities())
        self.assertFalse(upi._is_complete_indian_address('X', 'Kochi', 'KL', '682031'))

    def test_missing_or_malformed_file_falls_back_without_raising(self):
        garbage = self.write_pool('{ this is not json', name='garbage.json')
        absent = os.path.join(tempfile.gettempdir(), 'definitely-absent-pool.json')
        for path in (absent, garbage):
            with self.subTest(path=path):
                os.environ['UPI_BILLING_POOL_FILE'] = path
                upi._reset_upi_billing_row_caches()
                # 读不出内容时不报错，只是没有额外行
                self.assertEqual(upi._external_billing_address_rows(), [])
                # 内置池的成员关系不受影响
                self.assertEqual(upi._is_complete_indian_address(*MUMBAI_ROW), True)

    def test_loaded_rows_survive_dedup_against_the_builtin_pool(self):
        # 与内置池重复的行只保留一条
        path = self.write_pool({'addresses': [
            {'line1': 'Apollo Bunder, Colaba', 'city': 'Mumbai', 'state': 'MH', 'postal_code': '400001'},
        ]})
        with patch.object(upi, 'UPI_BILLING_ADDRESS_POOL', (MUMBAI_ROW,)), \
                patch.object(resolver, '_BUILTIN_PUBLIC_ADDRESSES', {'IN': []}):
            rows = self.rows_with(path)
        self.assertEqual(rows, [MUMBAI_ROW])

    def test_repeated_reads_use_the_cached_rows(self):
        path = self.write_pool({'addresses': [
            {'line1': '64 Shivaji Road, Kasba Peth', 'city': 'Pune', 'state': 'MH', 'postal_code': '411001'},
        ]})
        os.environ['UPI_BILLING_POOL_FILE'] = path
        upi._reset_upi_billing_row_caches()
        expected = upi._external_billing_address_rows()
        self.assertTrue(expected)
        # 第二次读取命中缓存，不再触碰文件
        with patch.object(upi, '_read_billing_row_source', side_effect=AssertionError('rows must be cached')):
            self.assertEqual(upi._external_billing_address_rows(), expected)


class BillingNormalizationTests(OfflineBillingTestCase):
    def test_address_fields_reject_objects_lists_and_booleans(self):
        for key in ('line1', 'line2', 'city', 'state', 'postal_code', 'country'):
            for invalid in ({'street': 'Example'}, ['Example'], True):
                with self.subTest(key=key, value=invalid):
                    billing = caller_billing()
                    billing['address'][key] = invalid
                    with self.assertRaisesRegex(ValueError, 'UPI 账单地址'):
                        upi.build_upi_billing('account@example.test', billing)

    def test_integer_postal_code_remains_supported(self):
        billing = caller_billing()
        billing['address']['postal_code'] = 400026
        self.assertEqual(upi.build_upi_billing('account@example.test', billing)['postal_code'], '400026')

    def test_valid_caller_address_is_preserved_without_consuming_pool(self):
        billing = caller_billing()
        original = copy.deepcopy(billing)
        with patch.object(upi, '_random_upi_billing_address', side_effect=AssertionError('Must preserve caller address')):
            result = upi._normalize_upi_billing(billing, 'account@example.test')
        self.assertEqual(billing, original)
        self.assertEqual(result, {
            'name': 'Billing Test', 'email': 'billing@example.test',
            'line1': 'Customer supplied street', 'line2': 'Unit 12',
            'city': 'Mumbai', 'state': 'Maharashtra', 'postal_code': '400026', 'country': 'IN',
        })

    def test_flat_caller_address_accepts_postal_alias_and_omitted_country(self):
        billing = caller_billing()
        flat = {'name': billing['name'], **billing['address']}
        flat['postal'] = flat.pop('postal_code')
        del flat['country']
        with patch.object(upi, '_random_upi_billing_address', side_effect=AssertionError('Must preserve caller address')):
            result = upi.build_upi_billing('account@example.test', flat)
        self.assertEqual(result['postal_code'], '400026')
        self.assertEqual(result['state'], 'Maharashtra')
        self.assertEqual(result['email'], 'account@example.test')
        self.assertEqual(result['line2'], 'Unit 12')

    def test_state_full_name_and_country_name_are_canonicalized(self):
        billing = caller_billing()
        billing['address'].update(state='maharashtra', country='India')
        result = upi._normalize_upi_billing(billing, '')
        self.assertEqual(result['state'], 'Maharashtra')
        self.assertEqual(result['country'], 'IN')

    def test_invalid_explicit_addresses_fail_without_silent_replacement(self):
        for fields in (
            {'state': 'KA'}, {'postal_code': '110001'}, {'country': 'US'},
            {'line1': ''}, {'city': ''}, {'state': 'ZZ'},
        ):
            with self.subTest(fields=fields):
                billing = caller_billing()
                billing['address'].update(fields)
                with patch.object(upi, '_random_upi_billing_address', side_effect=AssertionError('No replacement')):
                    with self.assertRaisesRegex(ValueError, 'UPI 账单地址'):
                        upi._normalize_upi_billing(billing, '')

    def test_partial_address_is_an_error_including_orphan_line2(self):
        for billing in (
            {'line1': 'Customer street'},
            {'country': 'US'},
            {'address': {'city': 'Mumbai'}},
            {'address': {'line2': 'Old unit'}},
        ):
            with self.subTest(billing=billing):
                with self.assertRaisesRegex(ValueError, 'UPI 账单地址'):
                    upi._normalize_upi_billing(billing, 'account@example.test')

    def test_missing_address_uses_one_complete_pool_row_and_keeps_contact_fields(self):
        selected = {
            'name': 'Pool Test', 'email': '',
            'address': dict(zip(('line1', 'city', 'state', 'postal_code'), BENGALURU_ROW)),
        }
        with patch.object(upi, '_random_upi_billing_address', return_value=selected) as choose:
            result = upi._normalize_upi_billing(
                {'name': 'Caller Name', 'email': 'caller@example.test'},
                'account@example.test', preferred_city='Bengaluru',
            )
        choose.assert_called_once_with(preferred_city='Bengaluru')
        self.assertEqual(result['name'], 'Caller Name')
        self.assertEqual(result['email'], 'caller@example.test')
        self.assertEqual(result['line1'], BENGALURU_ROW[0])
        self.assertEqual(result['city'], 'Bengaluru')
        self.assertEqual(result['state'], 'Karnataka')
        self.assertEqual(result['postal_code'], '560025')
        self.assertEqual(result['country'], 'IN')
        self.assertEqual(result['line2'], '')

    def test_country_hint_or_empty_address_does_not_require_a_custom_address(self):
        selected = {
            'name': 'Pool Test', 'email': '',
            'address': dict(zip(('line1', 'city', 'state', 'postal_code'), MUMBAI_ROW)),
        }
        for billing in (None, {}, {'address': {}}, {'country': 'IN'}, {'address': {'country': 'India'}}):
            with self.subTest(billing=billing):
                with patch.object(upi, '_random_upi_billing_address', return_value=selected) as choose:
                    result = upi._normalize_upi_billing(billing, 'account@example.test')
                choose.assert_called_once()
                self.assertEqual(result['line1'], MUMBAI_ROW[0])
                self.assertEqual(result['country'], 'IN')
                self.assertEqual(result['email'], 'account@example.test')


class BillingExtractionBoundaryTests(OfflineBillingTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'UPI_BILLING_SYNC_CITY': '0'}))
        self.enterContext(patch.object(upi, '_EXIT_CITY_BY_PROXY', {}))
        self.client = self.enterContext(patch.object(upi, 'UpiHttpClient'))
        self.auth = self.enterContext(patch.object(upi, 'resolve_upi_access_token', return_value='fixture-token'))
        self.enterContext(patch.object(upi, '_jwt_email', return_value='account@example.test'))
        self.promo = self.enterContext(patch.object(upi, '_fetch_promo_coupon_state', return_value='unknown'))
        self.attempt = self.enterContext(patch.object(upi, '_run_upi_attempt', side_effect=lambda *a, **k: {'ok': True}))
        self.probe = self.enterContext(patch.object(upi, '_probe_upi_exit_city', return_value='Mumbai'))
        self.build = self.enterContext(patch.object(upi, 'build_upi_billing', wraps=upi.build_upi_billing))
        self.enterContext(patch.object(upi, '_human_pause', side_effect=AssertionError('Unexpected retry')))

    def extract(self, proxy='http://first-proxy.invalid:8080', billing=None):
        return upi.extract_upi_link('fixture-token', proxy, billing=billing)

    def test_disabled_city_sync_never_probes_for_default_billing(self):
        self.assertTrue(self.extract()['ok'])
        self.probe.assert_not_called()
        self.assertEqual(self.build.call_args.kwargs['preferred_city'], '')
        self.assertEqual(self.attempt.call_args.kwargs['billing']['country'], 'IN')

    def test_valid_custom_billing_skips_probe_even_when_city_sync_is_enabled(self):
        os.environ['UPI_BILLING_SYNC_CITY'] = '1'
        billing = caller_billing()
        self.assertTrue(self.extract(billing=billing)['ok'])
        self.probe.assert_not_called()
        normalized = self.attempt.call_args.kwargs['billing']
        self.assertEqual(normalized['line1'], billing['address']['line1'])
        self.assertEqual(normalized['line2'], billing['address']['line2'])
        self.assertEqual(normalized['postal_code'], '400026')

    def test_enabled_city_sync_reuses_probe_and_passes_preference_to_billing(self):
        os.environ['UPI_BILLING_SYNC_CITY'] = '1'
        for _ in range(2):
            self.assertTrue(self.extract()['ok'])
        self.probe.assert_called_once_with('http://first-proxy.invalid:8080')
        self.assertEqual(self.build.call_count, 2)
        for invocation in self.build.call_args_list:
            self.assertEqual(invocation.kwargs['preferred_city'], 'Mumbai')
        for invocation in self.attempt.call_args_list:
            self.assertEqual(invocation.kwargs['billing']['city'], 'Mumbai')

    def test_enabled_city_sync_cache_keeps_proxies_separate(self):
        os.environ['UPI_BILLING_SYNC_CITY'] = '1'
        first = 'http://first-proxy.invalid:8080'
        second = 'http://second-proxy.invalid:8080'
        self.probe.side_effect = {first: 'Mumbai', second: 'Bengaluru'}.__getitem__
        for proxy in (first, second, first):
            self.assertTrue(self.extract(proxy)['ok'])
        self.assertEqual(self.probe.call_args_list, [call(first), call(second)])
        self.assertEqual(
            [invocation.kwargs['preferred_city'] for invocation in self.build.call_args_list],
            ['Mumbai', 'Bengaluru', 'Mumbai'],
        )
        self.assertEqual(
            [invocation.kwargs['billing']['city'] for invocation in self.attempt.call_args_list],
            ['Mumbai', 'Bengaluru', 'Mumbai'],
        )

    def test_invalid_custom_billing_is_rejected_before_any_external_work(self):
        os.environ['UPI_BILLING_SYNC_CITY'] = '1'
        billing = caller_billing()
        billing['address']['state'] = 'KA'
        with self.assertRaisesRegex(ValueError, 'UPI 账单地址'):
            self.extract(billing=billing)
        for external in (self.client, self.auth, self.promo, self.probe, self.attempt):
            external.assert_not_called()
        self.build.assert_not_called()


class ReverseAddressResolverTests(OfflineBillingTestCase):
    """代理坐标反查：只做字段提取、缓存与失败回退，不发真实请求。"""

    def setUp(self):
        super().setUp()
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.enterContext(
            patch.object(
                resolver,
                '_REVERSE_CACHE_PATH',
                os.path.join(directory, 'nominatim_reverse_cache.json'),
            )
        )
        self.enterContext(patch.object(resolver, '_LAST_NOMINATIM_AT', 0.0))
        self.enterContext(patch.object(resolver.time, 'sleep', return_value=None))

    def responder(self, payload, status=200):
        response = MagicMock()
        response.status_code = status
        response.json.return_value = payload
        return response

    def test_city_state_mapping_normalizes_the_indian_state_name(self):
        for code, state, city, postal in (
            ('DL', 'Delhi', 'New Delhi', '110001'),
            ('KA', 'Karnataka', 'Bengaluru', '560025'),
            ('MH', 'Maharashtra', 'Mumbai', '400001'),
            ('TN', 'Tamil Nadu', 'Chennai', '600034'),
            ('WB', 'West Bengal', 'Kolkata', '700016'),
        ):
            for state_value in (code, state):
                with self.subTest(state=state_value):
                    fields = upi._reverse_address_fields({'address': {
                        'line1': 'Fixture street', 'city': city,
                        'state': state_value, 'postal_code': postal,
                    }})
                    self.assertEqual(fields['state'], code)
                    self.assertEqual(fields['city'], city)

    def test_reverse_address_outside_the_five_states_falls_back_to_the_pool(self):
        """反查地址也受五地区限制，不能把被丢弃的地点名混入回退账单。"""
        reverse = {
            'name': 'Some Place',
            'address': {
                'line1': '12 MG Road', 'city': 'Kochi', 'state': 'Kerala',
                'postal_code': '682031', 'country': 'IN',
            },
        }
        for state in ('Kerala', 'KL', 'Gujarat', 'GJ', 'Uttar Pradesh', 'UP'):
            with self.subTest(state=state):
                reverse['address']['state'] = state
                self.assertIsNone(upi._reverse_address_fields(reverse))
                with patch.object(upi, '_random_upi_billing_address', wraps=upi._random_upi_billing_address) as pool:
                    billing = upi.build_upi_billing(
                        'account@example.test', None, reverse_address=reverse
                    )
                pool.assert_called_once()
                self.assertIn(upi._upi_state_code(billing['state']), upi.UPI_BILLING_STATES)
                self.assertNotEqual(billing['name'], 'Some Place')

    def test_reverse_place_name_is_never_used_as_the_billing_name(self):
        """Nominatim 的 name 是商户/地标名，不是人名，不能进账单姓名。"""
        place_name = 'Sri Krishna Sweets'
        reverse = {
            'name': place_name,
            'address': {
                'line1': '12 MG Road', 'city': 'Bengaluru', 'state': 'Karnataka',
                'postal_code': '560025', 'country': 'IN',
            },
        }
        billing = upi.build_upi_billing(
            'account@example.test', None, reverse_address=reverse
        )
        # 地址来自反查，姓名必须另生成。
        self.assertEqual(billing['line1'], '12 MG Road')
        self.assertEqual(billing['city'], 'Bengaluru')
        self.assertNotEqual(billing['name'], place_name)
        self.assertIn(billing['name'], upi.UPI_BILLING_NAMES)

    def test_reverse_result_carries_no_name_field(self):
        """反查结果本身就不该带 name，避免下游把它当姓名。"""
        geo = {'country': 'IN', 'latitude': '12.9', 'longitude': '77.6'}
        with patch.object(upi, '_probe_upi_exit_geo', return_value=geo), patch.object(
            resolver, 'resolve_reverse_address', return_value={
                'name': 'Sri Krishna Sweets',
                'line1': '12 MG Road', 'city': 'Bengaluru',
                'state': 'Karnataka', 'postal_code': '560025',
            }
        ):
            result = upi._reverse_billing_from_proxy('http://proxy.invalid:8080')
        self.assertNotIn('name', result)
        self.assertEqual(result['address']['city'], 'Bengaluru')

    def test_caller_name_still_wins_over_the_generated_one(self):
        reverse = {
            'name': 'Sri Krishna Sweets',
            'address': {
                'line1': '12 MG Road', 'city': 'Bengaluru', 'state': 'Karnataka',
                'postal_code': '560025', 'country': 'IN',
            },
        }
        billing = upi.build_upi_billing(
            'account@example.test', caller_billing(), reverse_address=reverse
        )
        self.assertEqual(billing['name'], 'Billing Test')

    def test_incomplete_reverse_result_falls_back_to_the_pool(self):
        reverse = {
            'address': {
                'line1': '12 MG Road', 'city': 'Bengaluru', 'state': 'Karnataka',
                'postal_code': '', 'country': 'IN',
            }
        }
        with patch.object(upi, '_random_upi_billing_address', wraps=upi._random_upi_billing_address) as pool:
            billing = upi.build_upi_billing(
                'account@example.test', None, reverse_address=reverse
            )
        pool.assert_called_once()
        self.assertEqual(billing['country'], 'IN')

    def test_caller_address_wins_over_the_reverse_result(self):
        reverse = {
            'address': {
                'line1': 'Reverse street', 'city': 'Kochi', 'state': 'Kerala',
                'postal_code': '682031', 'country': 'IN',
            }
        }
        billing = upi.build_upi_billing(
            'account@example.test', caller_billing(), reverse_address=reverse
        )
        self.assertEqual(billing['line1'], 'Customer supplied street')
        self.assertEqual(billing['postal_code'], '400026')

    def test_valid_coordinates_are_mapped_and_then_served_from_cache(self):
        payload = {
            'display_name': '12, MG Road, Kochi, Kerala, India',
            'name': 'Some Shop',
            'address': {
                'country_code': 'in', 'road': 'MG Road', 'house_number': '12',
                'city': 'Kochi', 'state': 'Kerala', 'postcode': '682031',
            },
        }
        with patch.object(
            resolver.requests, 'get', return_value=self.responder(payload)
        ) as request:
            first = resolver.resolve_reverse_address(9.9312, 76.2673, 'IN')
            second = resolver.resolve_reverse_address(9.9312, 76.2673, 'IN')
        self.assertEqual(first['line1'], '12 MG Road')
        self.assertEqual(first['city'], 'Kochi')
        self.assertEqual(first['source'], 'openstreetmap_nominatim_reverse')
        self.assertEqual(second, first)
        self.assertEqual(request.call_count, 1)

    def test_foreign_country_and_invalid_coordinates_are_rejected(self):
        payload = {'address': {'country_code': 'us', 'road': 'X', 'city': 'New York'}}
        with patch.object(resolver.requests, 'get', return_value=self.responder(payload)):
            self.assertIsNone(resolver.resolve_reverse_address(40.7, -74.0, 'IN'))
        for lat, lon in (('', ''), ('x', '1'), (91.0, 0.0), (0.0, 181.0)):
            with self.subTest(latitude=lat, longitude=lon):
                self.assertIsNone(resolver.resolve_reverse_address(lat, lon, 'IN'))

    def test_missing_city_or_street_is_not_a_usable_address(self):
        payload = {'address': {'country_code': 'in', 'road': 'MG Road'}}
        with patch.object(resolver.requests, 'get', return_value=self.responder(payload)):
            self.assertIsNone(resolver.resolve_reverse_address(9.9, 76.2, 'IN'))

    def test_network_failure_returns_none_instead_of_raising(self):
        with patch.object(
            resolver.requests, 'get', side_effect=RuntimeError('proxy unreachable')
        ):
            self.assertIsNone(resolver.resolve_reverse_address(9.9, 76.2, 'IN'))

    def test_non_indian_proxy_exit_never_triggers_a_lookup(self):
        geo = {'country': 'US', 'city': 'New York', 'latitude': '40.7', 'longitude': '-74.0'}
        with patch.object(upi, '_probe_upi_exit_geo', return_value=geo), patch.object(
            resolver, 'resolve_reverse_address'
        ) as lookup:
            self.assertEqual(upi._reverse_billing_from_proxy('http://proxy.invalid:8080'), {})
        lookup.assert_not_called()

    def test_proxy_lookup_discards_cached_or_fresh_results_outside_the_five_states(self):
        geo = {'country': 'IN', 'latitude': '9.9', 'longitude': '76.2'}
        with patch.object(upi, '_probe_upi_exit_geo', return_value=geo):
            for state, accepted in (('Kerala', False), ('KL', False), ('Tamil Nadu', True), ('TN', True)):
                with self.subTest(state=state), patch.object(resolver, 'resolve_reverse_address', return_value={
                    'line1': 'Fixture street', 'city': 'Fixture city',
                    'state': state, 'postal_code': '625001',
                }):
                    result = upi._reverse_billing_from_proxy('http://proxy.invalid:8080')
                    self.assertEqual(bool(result), accepted)

    def test_reverse_flag_can_be_switched_off(self):
        for value, expected in (
            ('0', False), ('false', False), ('no', False), ('off', False),
            ('1', True), ('true', True), ('', True),
        ):
            with self.subTest(value=value):
                with patch.dict(os.environ, {'UPI_BILLING_REVERSE': value}):
                    self.assertEqual(upi._upi_billing_reverse_enabled(), expected)


class ReverseBillingExtractionTests(OfflineBillingTestCase):
    """反查地址必须真的进入建单用的账单，而不是只让单元函数返回正确。"""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'UPI_BILLING_SYNC_CITY': '0'}))
        self.enterContext(patch.object(upi, '_EXIT_CITY_BY_PROXY', {}))
        self.enterContext(patch.object(upi, 'UpiHttpClient'))
        self.enterContext(patch.object(upi, 'resolve_upi_access_token', return_value='fixture-token'))
        self.enterContext(patch.object(upi, '_jwt_email', return_value='account@example.test'))
        self.enterContext(patch.object(upi, '_fetch_promo_coupon_state', return_value='unknown'))
        self.attempt = self.enterContext(
            patch.object(upi, '_run_upi_attempt', side_effect=lambda *a, **k: {'ok': True})
        )
        self.enterContext(patch.object(upi, '_human_pause', side_effect=AssertionError('Unexpected retry')))

    @staticmethod
    def reverse_result():
        return {
            'address': {
                'line1': 'Fixture street', 'city': 'Bengaluru', 'state': 'Karnataka',
                'postal_code': '560025', 'country': 'IN',
            },
            '_address_source': 'openstreetmap_nominatim_reverse',
        }

    def test_reverse_address_reaches_the_attempt_billing(self):
        with patch.object(upi, '_reverse_billing_from_proxy', return_value=self.reverse_result()):
            result = upi.extract_upi_link('fixture-token', 'http://proxy.invalid:8080')
        self.assertTrue(result['ok'])
        billing = self.attempt.call_args.kwargs['billing']
        self.assertEqual(billing['line1'], 'Fixture street')
        self.assertEqual(billing['city'], 'Bengaluru')
        self.assertEqual(billing['state'], 'Karnataka')
        self.assertEqual(billing['postal_code'], '560025')
        # 姓名必须来自姓名库，不能是地点名，也不能是空的。
        self.assertIn(billing['name'], upi.UPI_BILLING_NAMES)
        # 结果字典保持既有契约，不携带内部来源标记。
        self.assertNotIn('_address_source', billing)
        address_steps = [s for s in result['steps'] if s['name'] == '账单地址']
        self.assertIn('source=openstreetmap_nominatim_reverse', address_steps[-1]['detail'])

    def test_outside_region_never_reaches_checkout_or_claims_reverse_source(self):
        reverse = self.reverse_result()
        reverse['address'].update(city='Kochi', state='Kerala', postal_code='682031')
        with patch.object(upi, '_reverse_billing_from_proxy', return_value=reverse):
            result = upi.extract_upi_link('fixture-token', 'http://proxy.invalid:8080')
        billing = self.attempt.call_args.kwargs['billing']
        self.assertIn(upi._upi_state_code(billing['state']), upi.UPI_BILLING_STATES)
        self.assertNotEqual(billing['city'], 'Kochi')
        address_steps = [step for step in result['steps'] if step['name'] == '账单地址' and step['status'] == 'info']
        self.assertEqual(len(address_steps), 1)
        self.assertIn('source=upi_billing_pool', address_steps[0]['detail'])
        self.assertNotIn('openstreetmap_nominatim_reverse', address_steps[0]['detail'])

    def test_caller_address_short_circuits_the_reverse_lookup(self):
        with patch.object(upi, '_reverse_billing_from_proxy') as reverse:
            self.assertTrue(
                upi.extract_upi_link(
                    'fixture-token', 'http://proxy.invalid:8080', billing=caller_billing()
                )['ok']
            )
        reverse.assert_not_called()
        self.assertEqual(self.attempt.call_args.kwargs['billing']['line1'], 'Customer supplied street')

    def test_disabled_flag_skips_the_reverse_lookup_and_uses_the_pool(self):
        os.environ['UPI_BILLING_REVERSE'] = '0'
        with patch.object(upi, '_reverse_billing_from_proxy') as reverse:
            self.assertTrue(upi.extract_upi_link('fixture-token', 'http://proxy.invalid:8080')['ok'])
        reverse.assert_not_called()
        billing = self.attempt.call_args.kwargs['billing']
        self.assertEqual(billing['country'], 'IN')
        self.assertIn(billing['state'], ('Delhi', 'Karnataka', 'Maharashtra', 'Tamil Nadu', 'West Bengal'))

    def test_failed_reverse_lookup_falls_back_to_the_pool(self):
        with patch.object(upi, '_reverse_billing_from_proxy', return_value={}):
            self.assertTrue(upi.extract_upi_link('fixture-token', 'http://proxy.invalid:8080')['ok'])
        billing = self.attempt.call_args.kwargs['billing']
        self.assertIn(billing['state'], ('Delhi', 'Karnataka', 'Maharashtra', 'Tamil Nadu', 'West Bengal'))


if __name__ == '__main__':
    unittest.main()
