# How Forwarding Address differs from its nearest neighbours

The public gallery of the Build with CMC: API Hackathon was read on 2026-09-18. These are the
entries closest to this one — by track, by endpoint family, or by author — with the exact
boundary between each and Forwarding Address. Every entry is described from its own public
BUIDL page; pages are editable until the deadline, so a description may have moved since.

The one-line version: **Forwarding Address is the only entry that takes a single DEX liquidity
event and follows the wallet behind it across every pool of the token and every EVM chain the
asset trades on, then rewrites the alert's own severity** — using `/v1/dex/liquidity-change/list`
with `maker=` as a server-side join. Nobody else reads that endpoint.

| Entry | What it does | The exact boundary with ours |
|---|---|---|
| **Argus** — [dorahacks.io/buidl/48875](https://dorahacks.io/buidl/48875) · AI Agents and Automation | An anomaly detector with an 18-tool Claude agent and a deterministic `explain_move` that splits a coin's move into beta-to-BTC, sector excess and coin-specific residual, over ~20 endpoints (quotes, OHLCV, categories, liquidations, fear-and-greed, content), with an evidence panel and a rehearsed Basic-tier credit budget. | Argus explains **why a price moved** from index-level, CEX-listed aggregates and needs a keyed budget. Forwarding Address explains **where one wallet's liquidity went** from per-event DEX rows, keyless, 0 credits. No endpoint in common: Argus does not read the DEX family; we read nothing outside it except `/v2/cryptocurrency/info` for the cross-chain contract registry. |
| **CMC-Alpha-Terminal** — [dorahacks.io/buidl/48301](https://dorahacks.io/buidl/48301) · AI Agents and Automation | A C++20 core with five FastMCP tools over `listings/latest`, `quotes/latest` and `global-metrics`, presented in a local ANSI terminal; its default demo path is an offline mock engine. | Its tools return the endpoints' own fields to a model. Ours joins two rows the API never joins (a wallet's remove and its add in a different pool), adjudicates between five outcomes, and the default path is a live keyless run — there is no offline mode on the judged path. |
| **CMC Agent Tools** — [dorahacks.io/buidl/48789](https://dorahacks.io/buidl/48789) · AI Agents and Automation | An MCP toolbox exposing CoinMarketCap endpoints to agents (described from its pitch; no deployment was visible when read). | A toolbox is a surface; Forwarding Address is one investigation with a verdict, and the MCP server is one of three ways to reach it. The tool an agent gets from us — `where_did_liquidity_go` — answers a question no single endpoint answers. |
| **Precedent Panel** — [dorahacks.io/buidl/48668](https://dorahacks.io/buidl/48668) · AI Agents and Automation | A tool priced per call through CoinMarketCap's x402 pay-per-call surface (described from its pitch). | Its differentiator is how an agent **pays** for market data. Ours is what the agent **decides** with it, on a surface that costs nothing: every call is on the keyless `/public-api` host. |
| **Baserate** — [dorahacks.io/buidl/48763](https://dorahacks.io/buidl/48763) · Markets and Trading Tools | A backtested screener over 760 days of top-1000 snapshots that publishes the base rate of each screen, with per-token DEX wallet concentration as a sidebar on the token page. | Baserate uses DEX wallet data as **context on a token**; it never follows a wallet across pools or classifies a liquidity event. Our base rate is the inverse shape — 335 removals, each wallet followed — and it measures the alert, not the screen. |
| **Elephant Tracks** — [dorahacks.io/buidl/48344](https://dorahacks.io/buidl/48344) · Data and Visualisation · **same author** | Splits the per-swap feed (`/v1/dex/tokens/transactions`) by maker address to show who owns each side of a token's flow — one wallet as 62% of a sell side that "net flow" reported as balanced. | Same author, same family of field (the maker address on a DEX row), different endpoint and different question. Elephant Tracks reads **swaps** and reports a **share of a side**; Forwarding Address reads **liquidity events** (`/v1/dex/liquidity-change/list?maker=`) and reports **a verdict on one wallet's next move** — rebalance, migration, partial or exit — then rewrites an alert. No code is shared on the judged path; `forwarding.py` is stdlib-only and standalone. |

Not listed: entries in other tracks that read the canonical `listings`/`quotes`/`global-metrics`
endpoints or the derivatives and RWA families. They share no endpoint and no question with this
one.
