from __future__ import annotations

import copy
import os
import unittest
from unittest.mock import call, patch

import billing_address_resolver as resolver
import upi_link_service as upi


MUMBAI_ROW = ('Apollo Bunder, Colaba', 'Mumbai', 'MH', '400001')
BENGALURU_ROW = ('1 Residency Road', 'Bengaluru', 'KA', '560025')


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
            ('Gurgaon', 'HR', '122001'),
        ):
            with self.subTest(city=city):
                self.assertTrue(upi._is_complete_indian_address('Customer street', city, state, postal))

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
        ):
            self.assertCountEqual(upi._upi_billing_address_rows(), [MUMBAI_ROW, BENGALURU_ROW])

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


if __name__ == '__main__':
    unittest.main()
