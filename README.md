# price-tracker-mcp

Price checking + affiliate link MCP server (streamable HTTP) + REST API. Connector #3 in the monetized connector portfolio.

## Tools

| Tool | What it does |
|---|---|
| `check_price(url)` | Fetch a product page and extract its current price → `{price, currency, title, availability, source}`. Sources tried in order: Open Graph meta tags, JSON-LD offers, common price selectors. SSRF-guarded. |
| `find_best_price(query \| url \| upc, condition="new", country="US", max_results=10, path="direct")` | v2: compare one product across reputable stores (Tier A official APIs: Best Buy, Walmart, eBay — each live only when its key is set; Tier B: the v1 extractor on a supplied URL) → ranked offers with per-offer evidence and a verdict. A `best_offer` winner is named only on a high-confidence product match with a known delivered total; otherwise the verdict abstains (`best_found_total_unverified` / `no_confident_match`). $0.03/call in both paths. Broker layer: `path="broker"` affiliate-tags the winning offer's link when IOConnex is enrolled in that retailer's affiliate program (per-offer `affiliate` flag, `take_disclosure` in every response); ranking and prices never depend on the path or on commissions. |
| `make_affiliate_link(url, tag, program="amazon")` | Append an affiliate tracking tag to a product URL. `program="amazon"` sets the `tag` param; any other valid name sets a custom query param. |

## Endpoints

- `GET /health` → `{"status","version","x402"}`
- `POST /price` → `{"url"}` → `{"price","currency","title","availability","source"}`
- `POST /best-price` → `{"query"|"url"|"upc", "condition"?, "country"?, "max_results"?, "path"?}` → ranked best-price comparison (see `find_best_price`); `POST /paid/best-price` is the always-paid alias
- `POST /affiliate` → `{"url","tag","program"}` → `{"url"}`
- `POST /mcp` → MCP over streamable HTTP (stateless)

60/min per-IP rate limit (`RATE_LIMIT`). x402 micropayment metering is **facilitator-verified** (real EIP-3009 USDC authorizations on Base) and **live** on the hosted service since Oct 20, 2026 (`X402_ENABLED=false` by default for self-hosted runs). Stripe monthly plans via `/billing/*` are off by default too (`STRIPE_ENABLED=false`).

## Payments

Two independent, off-by-default payment paths:

- **x402 per-call**: `X402_ENABLED=true` + `X402_PAY_TO` (your Base wallet) + `X402_FACILITATOR_URL`. Clients send an `X-Payment` header (base64 JSON v1 payload); the server verifies with the facilitator, serves, then settles. Prices: `check_price` $0.008, `find_best_price` $0.03, `make_affiliate_link` $0.005, overridable via `X402_PRICE_CHECK_PRICE_USDC` / `X402_PRICE_FIND_BEST_PRICE_USDC` / `X402_PRICE_MAKE_AFFILIATE_LINK_USDC` (fallback `X402_PRICE_USDC=0.008`). JSON-RPC discovery methods stay free. The default facilitator (`https://x402.org/facilitator`) is **Base Sepolia testnet only** — mainnet needs `https://api.cdp.coinbase.com/platform/v2/x402` + CDP keys.
- **Stripe monthly**: `STRIPE_ENABLED=true` + `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` + `STRIPE_PRICE_ID`. `POST /billing/checkout` → Checkout Session; the webhook provisions a subscriber; `GET /billing/key?session_id=...` returns a one-time subscriber token. Send it as `X-Subscriber-Key` to bypass per-call charges. Only SHA-256 hashes of tokens are stored (SQLite, `BILLING_DB_PATH`).

Both paths are live on the hosted service; self-hosted runs still need their full config (see above) or they stay disabled.

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
| `X402_ENABLED` | `false` | **Live** on the hosted service since Oct 20, 2026. For self-hosted runs, configure the mainnet facilitator to enable |
| `RATE_LIMIT` | `60/minute` | e.g. `600/hour` |
| `X402_PRICE_USDC` / `X402_PAY_TO` | `0.008` / unset | Per-tool: `X402_PRICE_CHECK_PRICE_USDC` ($0.008), `X402_PRICE_FIND_BEST_PRICE_USDC` ($0.03), `X402_PRICE_MAKE_AFFILIATE_LINK_USDC` ($0.005) |
| `X402_FACILITATOR_URL` | `https://x402.org/facilitator` | **Sepolia testnet only**; mainnet = CDP facilitator URL + CDP keys |
| `STRIPE_ENABLED` | `false` | Needs `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID` |
| `BESTBUY_API_KEY` | unset | v2 Tier A: enables the Best Buy Products API source in `find_best_price` when set |
| `WALMART_CONSUMER_ID` / `WALMART_PRIVATE_KEY` / `WALMART_KEY_VERSION` | unset | v2 Tier A: enables the Walmart Affiliate API source when all three are set (private key is the affiliate RSA PEM) |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | unset | v2 Tier A: enables the eBay Browse API source when both are set |
| `<RETAILER>_AFFILIATE_TAG` (`BESTBUY_` / `WALMART_` / `EBAY_`) | unset | Broker layer: our affiliate tag/ID for that retailer's affiliate program. **Set = enrolled** — the broker path may tag that retailer's winning links; unset = `affiliate: false` even when the retailer's API key (above) is live. An API key is not an affiliate enrollment. |
| `<RETAILER>_AFFILIATE_PARAM` | program default (`ref` / `affp1` / `mkcid`) | Broker layer: query parameter the program expects the tag in, if it differs from the default |
| `<RETAILER>_AFFILIATE_RATE` | unset | Broker layer: commission rate the program publishes for us, disclosed verbatim as `take_rate_if_known`; never estimated |

## Deploy

See [DEPLOY.md](DEPLOY.md). One-click: `render.yaml` blueprint (Starter plan).

## Roadmap

- Watchlist + price-drop alerts (persistence + notifications)
- Monetization enablement (AZ LLC, mainnet facilitator, Stripe products) before flipping payments on

