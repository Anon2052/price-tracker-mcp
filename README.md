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

60/min per-IP rate limit (`RATE_LIMIT`). x402 micropayment metering is a **structural stub** behind `X402_ENABLED=false` — no on-chain verification.

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -e .
uvicorn price_tracker.rest_app:app --host 0.0.0.0 --port 8000
curl localhost:8000/health
```

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `PORT` / `HOST` | `8000` / `0.0.0.0` | `PORT` injected by the host |
| `X402_ENABLED` | `false` | **Leave off** — stub, not prod-ready |
| `RATE_LIMIT` | `60/minute` | e.g. `600/hour` |
| `X402_PRICE_USDC` / `X402_PAY_TO` | `0.015` / unset | Only relevant when x402 verification is real |

## Deploy

See [DEPLOY.md](DEPLOY.md). One-click: `render.yaml` blueprint (Starter plan).

## Roadmap

- Watchlist + price-drop alerts (persistence + notifications)
- Real x402 verification + Stripe before monetizing
