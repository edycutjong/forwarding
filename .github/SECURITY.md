# Security Policy

## Reporting a vulnerability
Open a [private security advisory](../../security/advisories/new), or email **edy.cu@live.com**.
Please do not open a public issue for anything exploitable. You will get an acknowledgment
within 48 hours and a resolution timeline after triage.

## Secrets posture
**This project ships no secrets, by design.** The judged code path — the CLI, the MCP server
and the hosted proxy — uses CoinMarketCap's keyless `/public-api` surface, so there is no API
key in the repository, in CI, or in the deployment: not hidden, simply not required.
`/api/health` on the deployment reports `key_exported: false`, and a live test asserts it.

An exported `CMC_API_KEY` is an optional escape hatch for a throttled IP. When one is present
the run announces itself on its first line, its last line and in its receipt, and the receipt
never carries the key's value.

## The boundaries are tested, not asserted

- `tests/test_fetch.py` — the default path sends no key header and uses the public surface; a
  keyed run is named so it can never pass as keyless; the receipt never carries the key.
- `tests/test_boundary.py` — no MCP tool takes a credential, a webhook or a URL, and no tool
  call can make the process send anything anywhere, however its arguments are padded; the
  hosted proxy accepts exactly a lowercase chain slug and a 40-hex EVM address, follows the
  wallet on that chain only, and answers only GET and the CORS preflight.
- `tests/test_investigate.py` — the plan-gated `startTime` parameter is never sent; the window
  is walked with the cursor.

## Scanning
`gitleaks` on every push over full history (`.gitleaks.toml` allowlists exactly one shape — a
20-byte EVM address, which the committed API receipts contain), CodeQL weekly and on every PR,
`pip-audit` over both requirement files in CI, Dependabot monthly.
