# price-tracker-mcp

Price checking + affiliate link MCP server (streamable HTTP) + REST API. Connector #3 in the monetized connector portfolio.

## Tools

| Tool | What it does |
|---|---|
| `check_price(url)` | Fetch a product page and extract its current price → `{price, currency, title, availability, source}`. Sources tried in order: Open Graph meta tags, JSON-LD offers, common price selectors. SSRF-guarded. |
| `make_affiliate_link(url, tag, program="amazon")` | Append an affiliate tracking tag to a product URL. `program="amazon"` sets the `tag` param; any other valid name sets a custom query param. |

## Endpoints

- `GET /health` → `{"status","version","x402"}`
- `POST /price` → `{"url"}` → `{"price","currency","title","availability","source"}`
- `POST /affiliate` → `{"url","tag","program"}` → `{"url"}`
- `POST /mcp` → MCP over streamable HTTP (stateless)

60/min per-IP rate limit (`RATE_LIMIT`). x402 micropayment metering is **facilitator-verified** (real EIP-3009 USDC authorizations on Base) behind `X402_ENABLED=false` — off by default. Stripe monthly plans via `/billing/*` are off by default too (`STRIPE_ENABLED=false`).

## Payments

Two independent, off-by-default payment paths:

- **x402 per-call**: `X402_ENABLED=true` + `X402_PAY_TO` (your Base wallet) + `X402_FACILITATOR_URL`. Clients send an `X-Payment` header (base64 JSON v1 payload); the server verifies with the facilitator, serves, then settles. Prices: `check_price` $0.02, `make_affiliate_link` $0.005, overridable via `X402_PRICE_CHECK_PRICE_USDC` / `X402_PRICE_MAKE_AFFILIATE_LINK_USDC` (fallback `X402_PRICE_USDC=0.015`). JSON-RPC discovery methods stay free. The default facilitator (`https://x402.org/facilitator`) is **Base Sepolia testnet only** — mainnet needs `https://api.cdp.coinbase.com/platform/v2/x402` + CDP keys.
- **Stripe monthly**: `STRIPE_ENABLED=true` + `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` + `STRIPE_PRICE_ID`. `POST /billing/checkout` → Checkout Session; the webhook provisions a subscriber; `GET /billing/key?session_id=...` returns a one-time subscriber token. Send it as `X-Subscriber-Key` to bypass per-call charges. Only SHA-256 hashes of tokens are stored (SQLite, `BILLING_DB_PATH`).

Do not enable either in production before the AZ LLC is formed.

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .
uvicorn price_tracker.rest_app:app --host 0.0.0.0 --port 8000
curl localhost:8000/health
```

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `PORT` / `HOST` | `8000` / `0.0.0.0` | `PORT` injected by the host |
| `X402_ENABLED` | `false` | **Leave off** until AZ LLC formed + mainnet facilitator configured |
| `RATE_LIMIT` | `60/minute` | e.g. `600/hour` |
| `X402_PRICE_USDC` / `X402_PAY_TO` | `0.015` / unset | Per-tool: `X402_PRICE_CHECK_PRICE_USDC` ($0.02), `X402_PRICE_MAKE_AFFILIATE_LINK_USDC` ($0.005) |
| `X402_FACILITATOR_URL` | `https://x402.org/facilitator` | **Sepolia testnet only**; mainnet = CDP facilitator URL + CDP keys |
| `STRIPE_ENABLED` | `false` | Needs `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID` |
| `SUBSCRIBER_MONTHLY_CREDIT_LIMIT` | `1000` | Pro subscriber fair-use cap (credits; 1 credit = $0.01 of x402 list price) |
| `FREE_TIER_ENABLED` | `true` | Kill switch for the anonymous monthly free bucket |
| `FREE_MONTHLY_CREDITS` | `50` | Monthly free credits per client IP (X-Forwarded-For, first entry) |
| `ADMIN_KEY` | unset | **Required** for `GET /billing/stats`; long random string, 401s everything until set |

## Deploy

See [DEPLOY.md](DEPLOY.md). One-click: `render.yaml` blueprint (Starter plan).

## Roadmap

- Watchlist + price-drop alerts (persistence + notifications)
- Monetization enablement (AZ LLC, mainnet facilitator, Stripe products) before flipping payments on
