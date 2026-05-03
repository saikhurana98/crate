# ADR-0002: Hexagonal / ports-and-adapters architecture

- **Status:** accepted
- **Date:** 2026-05-03

## Context

We have six download sources of three completely different shapes (HTTP scrapers, Telegram bots, P2P / Soulseek), multiple metadata resolvers (MusicBrainz, Spotify, Apple, YouTube Music), multiple cover-art fetchers, multiple audio verification techniques (chromaprint, FLAC health-check, fake-FLAC spectrum analysis), and likely more added over time. We also need:

1. **Test-first development** — every contract must be expressible as a failing test before any IO.
2. **Adapter swap-ability** — replacing one source backend with another (or adding a new one) must not require touching application logic.
3. **Provider-agnostic business logic** — the scheduler, verifier, and use-cases never know what backend they talk to.

## Decision

Adopt a strict hexagonal (ports-and-adapters) layout. Layer dependency direction is one-way: `composition → application → ports → domain` and `adapters` / `infrastructure` implement `ports`.

```
domain/         pure types, zero deps
ports/          interfaces, depends only on domain
application/    use-cases, depends on domain + ports
adapters/       implements ports, can depend on domain + ports + 3rd-party SDKs
infrastructure/ same as adapters but for IO concerns (sqlite, fs, ws, browser pool)
composition/    DI wiring; the only place that imports across layers
```

**Enforcement**: a `dependency-cruiser` or biome custom rule blocks any `application/**` import from `adapters/**` or `infrastructure/**`. CI fails on violation.

**Contract tests**: every port has a single test suite (`tests/ports/<port>.contract.ts`) parametrised over a list of adapter factories. To add a source, the developer registers the factory in the suite. Suite passes ⇒ adapter satisfies the contract.

## Consequences

- **Positive**: New source = one file in `adapters/sources/` + entry in the contract test parameter list. Application logic (scheduler, verifier, use-cases) never has to change. Tests pin behaviour, not implementation.
- **Negative / accepted trade-offs**: Up-front design cost is higher — we have to think hard about the port shape (`SourceAdapter`'s search/probe/fetch trio took several iterations). The indirection makes one-off scripts more verbose.
- **Neutral**: Composition root becomes the only file that knows everything; it ends up large but that's where complexity belongs.

## Alternatives considered

- **Plain layered architecture** (controller → service → repository): simpler, but couples business logic to one adapter shape per layer. New backend types (Telegram, P2P) would force ad-hoc shims.
- **Hexagonal but with decorators / DI container** (`tsyringe`, `inversify`): adds reflect-metadata runtime cost and a layer of magic. We prefer explicit constructor injection in `composition/container.ts` — it's longer but trivially debuggable.
- **Effect-TS-style algebraic effects**: powerful and Result-aware out of the box, but a heavy ecosystem buy-in. We go boring with `Promise<Result<T, E>>` returns and standard async/await.

## References

- Alistair Cockburn's original "ports and adapters" article.
- Vaughn Vernon, *Implementing Domain-Driven Design*, ch. 4 (architecture).
