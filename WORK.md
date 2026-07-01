# Work Tracker

Single source of truth for unit status. **Conductor agent is the only writer of status fields.** Workers may append notes under their unit but never edit `status`, `merged_at`, or `failure_count`.

Status values: `ready` (deps merged, awaiting pickup) · `in-progress` (worker spawned) · `merged` (PR merged) · `blocked-needs-human` (3 failures, escalated) · `pending-deps` (waiting on a dep)

---

## Phase 0 — Bootstrap

### 0-bootstrap
- **status:** ready
- **deps:** —
- **owner:** unassigned
- **branch:** —
- **pr:** —
- **failure_count:** 0
- **merged_at:** —

---

## Phase 1 — Domain, ports, protocol

### 1A-domain
- **status:** pending-deps
- **deps:** 0-bootstrap
- **owner:** unassigned
- **branch:** —
- **pr:** —
- **failure_count:** 0
- **merged_at:** —

### 1B-ports
- **status:** pending-deps
- **deps:** 0-bootstrap, 1A-domain
- **owner:** unassigned
- **branch:** —
- **pr:** —
- **failure_count:** 0
- **merged_at:** —

### 1C-protocol
- **status:** pending-deps
- **deps:** 0-bootstrap, 1A-domain
- **owner:** unassigned
- **branch:** —
- **pr:** —
- **failure_count:** 0
- **merged_at:** —

---

## Phase 2 — Contract test suites + fakes

### 2-contracts-fakes
- **status:** pending-deps
- **deps:** 1A-domain, 1B-ports, 1C-protocol
- **owner:** unassigned
- **branch:** —
- **pr:** —
- **failure_count:** 0
- **merged_at:** —

---

## Phase 3 — Adapters + infrastructure

### 3A-onthespot
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3B-ytdlp-ytmusic
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3C-squid-qobuz
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3D-lucida
- **status:** pending-deps
- **deps:** 2-contracts-fakes, 3P-browser-pool
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3E-monochrome
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3F-soulseek
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3G-musicbrainz
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3H-spotify
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3I-apple
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3J-ytmusic
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3K-coverart-misc
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3L-chromaprint
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3M-parsers
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3N-sqlite
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3O-nas-writer
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3P-browser-pool
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3Q-ws-server
- **status:** pending-deps
- **deps:** 1C-protocol, 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3R-ops
- **status:** pending-deps
- **deps:** 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 3S-slskd-shim
- **status:** pending-deps
- **deps:** 2-contracts-fakes, 4H-tier-cascade
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

---

## Phase 4 — Application use cases

### 4A-scheduler
- **status:** pending-deps
- **deps:** 1A-domain, 1B-ports, 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 4B-resolver-chains
- **status:** pending-deps
- **deps:** 1A-domain, 1B-ports, 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 4C-download-live-set
- **status:** pending-deps
- **deps:** 4A-scheduler, 4B-resolver-chains
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 4D-download-bulk
- **status:** pending-deps
- **deps:** 4A-scheduler, 4B-resolver-chains
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 4E-upgrade
- **status:** pending-deps
- **deps:** 4A-scheduler
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 4F-verification
- **status:** pending-deps
- **deps:** 1A-domain, 1B-ports, 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 4G-purge-daemon
- **status:** pending-deps
- **deps:** 1A-domain, 1B-ports, 2-contracts-fakes
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 4H-tier-cascade
- **status:** pending-deps
- **deps:** 4A-scheduler, 4B-resolver-chains, 4F-verification
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

---

## Phase 5 — Composition + entry point

### 5-composition
- **status:** pending-deps
- **deps:** all of Phase 3, all of Phase 4
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

---

## Phase 6 — Debug UI

### 6A-debug-ui-shell
- **status:** pending-deps
- **deps:** 1C-protocol
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

### 6B-debug-ui-components
- **status:** pending-deps
- **deps:** 6A-debug-ui-shell
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

---

## Phase 7 — Integration + smoke

### 7-integration
- **status:** pending-deps
- **deps:** 5-composition, 6B-debug-ui-components
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

---

## Phase 8 — Packaging (Docker)

### 8-docker
- **status:** pending-deps
- **deps:** 5-composition, 3S-slskd-shim
- **owner:** unassigned
- **branch:** —  **pr:** —  **failure_count:** 0  **merged_at:** —

---

## Conductor log

(Conductor appends timestamped entries here on each loop iteration.)
