# ADR-0012: Playwright browser pool as a managed resource

- **Status:** accepted
- **Date:** 2026-05-03

## Context

The service is intended to run headless. However, some sources we depend on (Lucida is the canonical example, possibly Monochrome) hide behind aggressive Cloudflare challenges, JavaScript-rendered pages, or session cookies that are painful to obtain via plain HTTP. For these adapters, real browser automation is the only practical path.

If each adapter spun its own Playwright instance:

- Cold-start cost (~1-2 seconds per Chromium launch) on every request.
- Memory blow-up: ten concurrent jobs = ten Chromium processes.
- No shared cookie / session storage across adapters that legitimately want to (e.g., a "stealth" profile that survived one captcha can serve subsequent requests).
- No central rate limit or memory ceiling.

We need a **shared pool** abstraction: adapters request a browser lease; the pool manages lifecycle, reuse, and limits.

## Decision

- **Port**: `BrowserSessionPool` in `ports/browser-session.ts`:

```ts
export interface BrowserSessionPool {
  acquire(opts: AcquireOpts, signal: AbortSignal): Promise<Result<BrowserLease, BrowserError>>;
}

export interface BrowserLease {
  readonly page: Page;        // Playwright Page handle
  release(): Promise<void>;   // mandatory; auto-released on signal abort
}

export interface AcquireOpts {
  readonly profile: "default" | "stealth" | "logged-in-spotify";
  readonly maxLeaseMs: number;
}
```

- **Implementation**: `infrastructure/browser/playwright-pool.ts`. One Playwright `BrowserContext` per profile (cookies, storage, perf reuse). `p-queue` per profile, default concurrency 2. Lease auto-released on `signal.abort()` or `maxLeaseMs` exceeded (whichever first). Periodic recycle of contexts above a memory threshold.
- **Headless by default**. Override for visual debugging via `LBMD_BROWSER_HEADED=1`.
- **Adapters declare their dependency** via `capabilities.requires.browser = true`. Composition root injects the pool only into adapters that need it. Pure-HTTP adapters (Squid mirror) do not see the pool.
- **Profiles** are a finite enum, not free-form strings, so the composition root can pre-create contexts at boot and keep cold start out of the request path.
- **Health**: pool exposes `inUse`, `available`, `restarts` counters via the metrics endpoint.

## Consequences

- **Positive**: Cold start cost (~1-2 s) paid once per profile at boot; each request reuses a warm context (~50 ms acquire). Memory ceiling is bounded by `concurrency × number-of-profiles`. Profile-level cookie reuse (e.g., Cloudflare clearance cookie) is automatic. Adapters that don't need a browser pay zero cost.
- **Negative / accepted trade-offs**: Playwright is a heavy dependency (Chromium binary ~150 MB on disk, ~200 MB RAM per context). Acceptable for a backbone service deployed once. We do not multi-tenant the pool across separate users; single-process assumption holds.
- **Neutral**: We could later add a "tor / proxy" profile for sources that geo-block; the port shape already supports it.

## Alternatives considered

- **Per-adapter browser instances**: simpler conceptually, terrible at scale. Already discussed above.
- **Headless HTTP scrapers (`got`, `node-fetch` + manual cookie jar)**: works for some sources but not the JS-heavy ones. We use plain HTTP wherever possible (Squid, Soulseek protocol, Telegram clients) and reserve Playwright for the few adapters that genuinely need it.
- **`puppeteer` instead of Playwright**: similar functionality; Playwright has better anti-bot resilience and multi-context primitives that fit the pool design.
- **Real-browser-as-a-service (Browserless, ScrapingBee)**: monetary cost, network round-trip for every page action; not worth it for self-hosted local-network deployment.

## References

- Playwright BrowserContext docs: https://playwright.dev/docs/browser-contexts
- `p-queue`: https://github.com/sindresorhus/p-queue
