# BLIK engine

Interactive Poland BLIK flow for the console. The engine fixes checkout,
billing and browser context to `PL` / `PLN` / `pl-PL` / `Europe/Warsaw`, keeps
one Playwright page alive while waiting for a six-digit code on stdin, and only
returns success after `/backend-api/subscriptions` reports an active Plus plan.

The Stripe.js page generates Radar, Sentinel approval and passive hCaptcha
materials. HAR secrets, checksums and one-time challenge values are never
hard-coded.

## Protocol

`src/extractor.js` runs the engine as a stdin/stdout child process:

```text
--at <access token> --proxy <url> [--email <mail>] [--fingerprint <json>]
[--promo-campaign-id plus-1-month-free] [--timeout 90] [--paid-timeout 300]
[--headless | --no-headless]
```

- stdout: one `[stage] [status] message` line per step plus JSON event lines
  such as `{"event":"blik_bank_authorization","attempt":1}`.
- stdin: `{"code":"123456"}` submits a fresh six-digit code. The engine asks
  again (up to three codes) when the bank rejects one.
- The process exits 0 only when `/backend-api/subscriptions` reports Plus and
  prints the final JSON result on stdout.

## Configuration

- `BLIK_CHATGPT_CLIENT_VERSION` / `BLIK_CHATGPT_CLIENT_BUILD` override the
  ChatGPT deployment identity the engine presents; the defaults come from the
  newest captured HAR.

## Tests

```bash
cd blik_engine && python -m unittest test_blik_service
```

## HAR reference

`docs/blik_har_analysis_2026-09-09.md` documents the successful BLIK checkout
the current request contract is aligned with.
