# RR Trading One — Phase 0: repository assessment and architecture report

Branch: `feature/rr-trading-one` (from `main` at `097adf8`). This phase changed **no code**. Earlier unfinished work
from the native-Windows plan is kept on `wip/windows-native-compat` (sandbox Job Object, Windows-safe sandbox
environment, Windows service stop signals, `agent-token` CLI; tested on Linux only).

## 1. Baseline (before any change)

| Item | Result |
|---|---|
| Engine tests (`backend`, PostgreSQL 16 + Redis) | **155 passed** |
| Original MT5 connector tests (`mt5-connector`) | **10 passed** |
| RR MT5 Connector desktop tests (`windows-connector`, Qt offscreen) | **24 passed** |
| Windows CI (`windows-latest`): tests, EXE, packaged self-test, installer, silent install/uninstall | **passed** (run 43246446464) |
| Frontend production build | passed (last built at `5cbd739`; frontend unchanged since) |
| Pre-existing failures | none |

## 2. Inventories

### 2.1 Strategies — the "196" reconciled
* The registry holds **215 strategies** in 12 categories. Their IDs, parameters and code hashes are frozen in
  `backend/tests/baseline/strategy_inventory.json` and enforced by `tests/test_preservation.py`.

  | Category | Count | Category | Count | Category | Count |
  |---|---|---|---|---|---|
  | SMC | 20 | indicators | 25 | fibonacci | 10 |
  | ICT | 20 | volume | 15 | reversal | 10 |
  | price action | 30 | sessions | 15 | quantitative | 20 |
  | liquidity | 20 | trend | 15 | combinations | 15 |

* **196 are enabled after setup**. That is the "196 production strategies" in the specification. The other 19:
  * 4 risky strategies, disabled by design: `quant_grid`, `quant_martingale`, `quant_anti_martingale`, `quant_dca`;
  * 3 off by default: `quant_hft`, `quant_market_making`, `quant_vol_arb`;
  * 12 unavailable, because their data feeds are missing (market depth, order flow, open interest, news feed,
    options implied volatility, portfolio allocator, trained model, Claude agent).
* **Plan:** preserve all 215, keep the 196/19 split, and add a test that asserts the 196 enabled set.

### 2.2 Platform components that already exist
| Component | Where | State |
|---|---|---|
| FastAPI backend, RBAC, CSRF, audit | `backend/app` | 112 HTTP routes, 2 WebSockets, 60 tables, 3 additive migrations (`0001`–`0003`) |
| Trading worker (scanner, auto-execution, startup checks, reconciliation) | `app/workers/main.py` | Single instance (Redis lock); `auto_trading=false` by default |
| AI Learner (separate process, never places orders) | `app/learning_engine/scheduler.py` | Idempotent jobs, retries, timeouts |
| Risk Engine, kill switch | `app/risk/engine.py`, `app/execution/manager.py` | Kill switch default position policy is **PROTECT** (ensures stop-losses, does **not** close all) |
| DEMO/LIVE separation | `trading_accounts.mode`, live approval, identity checks | LIVE needs administrator approval; identity mismatch → kill switch |
| Original MT5 connector (HTTPS service) | `mt5-connector/connector` | Journal, idempotency, HMAC, replay protection |
| RR MT5 Connector desktop agent | `windows-connector/rr_connector` | Terminal detection, attach to a logged-in terminal, guard, outbound relay; Windows CI green |
| Claude integration | `app/agents/claude_agent.py`, `app/agents/strategy_discovery` | Anthropic only |
| React console (24 pages) | `frontend` (Next.js 15) | Every page is a client component, so it can be **statically exported** and embedded in Tauri |

### 2.3 Specification ASIE components vs existing modules (reuse, do not duplicate)
| ASIE component | Existing implementation | Gap |
|---|---|---|
| 1 Performance Analyzer | `learning_engine/performance.py`: 13 dimensions, Wilson/bootstrap CIs, verdicts | — |
| 2 Strategy Ranking Engine | Per-strategy metrics exist | **Ranking with CI-aware ordering + UI** |
| 3 Adaptive Weight Optimizer | `consensus_weights.py`: validated, ≥200 OOS outcomes, approval required | Strategy-level weights: **shadow-only proposals** |
| 4 Market Regime Intelligence | `intelligence/regime.py` (HMM, CUSUM, benchmark) | — |
| 5 Correlation Analyzer | `/api/learning/performance/correlation`, strategy DNA fingerprints | UI page |
| 6 Multi-Timeframe Consensus | Scanner higher-timeframe context (`HTF_OF`) + specialist consensus | **Explicit cross-timeframe agreement score** |
| 7 Confidence Calibration | Isotonic calibrator, Brier/ECE, reliability diagrams | — |
| 8 Drift Detector | `learning_engine/drift.py` | — |
| 9 Walk-Forward Validation | Backtesting engine + experiment validation pipeline | — |
| 10 Shadow Evaluation | Shadow models, forward shadow tests for experiments | Shadow comparison vs production baseline for weight proposals |
| 11 Strategy Discovery | Claude discovery agent + AST-validated sandbox | Provider-independent (gateway) |
| 12 Research Approval Workflow | Lifecycle with approvals, rollback | — |

### 2.4 Gaps against the specification (new work)
1. **Desktop controller** (Tauri 2 + React + Rust supervisor, allowlisted operations, health-verified status).
2. **AI provider gateway**: Anthropic, Qwen (DashScope OpenAI-compatible API), Ollama-compatible; per-task
   routing, fallback, cost and token metering, audit metadata, encrypted keys.
3. **Worker lifecycle and execution-permission state machine** (STOPPED → … → AUTHORIZED) with start/stop/restart
   and all 10 startup verifications. Today the worker process runs whenever started, and execution is gated by
   `auto_trading` and the startup checks.
4. **Emergency stop: cancel eligible broker-side pending orders.** Today it rejects unsent orders but does not
   cancel pending orders already at the broker.
5. **MT5 Manager for multiple terminals and account profiles.** The desktop agent attaches to one terminal; it
   needs supervised headless mode, terminal start/stop and profile isolation.
6. **Backup encryption and a demonstrated restore.** `scripts/backup.sh` writes unencrypted `pg_dump`.
7. **Windows installer/updater for the whole platform** (EXE/MSI, signed updates, backup before upgrade,
   previous-version recovery).
8. **Desktop-only pages**: MT5 Manager, Trading Worker, AI Studio, System Monitor, Backup & Recovery.
9. **Manuals**: installation, administrator, user, backup and recovery, security architecture.

## 3. Windows compatibility report

| Area | Finding |
|---|---|
| MT5 connector and desktop agent | Windows-native, verified on Windows CI (MetaTrader5 5.0.6231, Credential Manager) |
| Backend Python dependencies | All have Windows wheels (psycopg, lightgbm, xgboost, numpy, scipy); `uvloop` is skipped on Windows automatically |
| Backend code | Linux-only in two places: the experimental-strategy sandbox (`resource` limits) and SIGTERM-only shutdown. Fixed on `wip/windows-native-compat`. Not needed if the backend runs in Linux containers. |
| Deployment scripts | `scripts/*.sh` and `nginx/` are Linux-only. Windows equivalents are required. |
| Frontend | Portable; static export is possible |
| **Docker Desktop on Windows Server 2022** | **Not supported.** Docker Desktop supports Windows 10/11 only. See the decision below. |
| WSL2 on Windows Server 2022 | Supported, but the VPS must expose **nested virtualization**, which many VPS plans don't |
| Tauri 2 on Windows Server 2022 | Needs the WebView2 runtime, which Server 2022 doesn't ship. The installer must bundle the WebView2 bootstrapper. |
| MT5 terminal | Runs on Server 2022. It must stay in a **logged-in interactive session**: disconnect RDP, don't sign out; use auto-logon for reboots. |

## 4. Blocking decision: infrastructure on Windows Server 2022

The specification requires Docker Compose with Docker Desktop and WSL2, but **Docker Desktop cannot be installed
on Windows Server 2022**. There are two safe alternatives:

| Option | How | Needs | Pros | Cons |
|---|---|---|---|---|
| **A. Docker Engine inside WSL2** (no Docker Desktop) | WSL2 Ubuntu on the VPS runs the existing `docker-compose.yml` (PostgreSQL, Redis, API, worker, learner). The Tauri app supervises it through `wsl.exe`. | Nested virtualization on the VPS | Same tested Linux stack as Ubuntu; least porting; meets the "Docker Compose" requirement | WSL2 networking/start-up must be supervised; not possible on many VPS plans |
| **B. Native Windows services** | PostgreSQL for Windows, a Redis-compatible server and Python services under the Tauri supervisor (the `wip/windows-native-compat` work) | Nothing special | Works on any Windows VPS; fewer moving parts | Backend runs natively on Windows (sandbox and service changes needed; partly done) |

Check whether the VPS supports option A by running this in PowerShell on the VPS:
```powershell
(Get-CimInstance Win32_Processor).VirtualizationFirmwareEnabled; (Get-ComputerInfo -Property HyperVRequirementVirtualizationFirmwareEnabled, HyperVisorPresent)
```
If `HyperVisorPresent` is True, or `wsl --install -d Ubuntu-24.04` succeeds and `wsl -l -v` shows **VERSION 2**,
option A is possible. **Recommendation:** use A if the VPS supports it; otherwise B.

## 5. Proposed architecture
```
RR Trading One (Tauri 2, runs in the interactive user session)
├─ Rust supervisor: allowlisted operations only (start/stop/restart named services, backup, restore,
│                   migrate, health), health-endpoint-verified status, restart limits, single instance
├─ React UI: existing console (static export) + new desktop pages; IPC to the supervisor, HTTPS to the API
├─ MT5 Manager sidecar: rr_connector core in headless mode, one per terminal profile, in the user session
└─ Infrastructure: option A (WSL2 Docker Engine) or B (native services) — PostgreSQL, Redis, API,
                   trading worker (STOPPED by default), AI Learner (never executes)
Data: C:\ProgramData\RR Trading One\{data,backups,logs,models,cache,secrets}   (ACL-restricted)
User: %APPDATA%\RR Trading One\user-settings
```
* The relay design already built (the engine never calls the PC; the agent dials out) carries over unchanged
  when everything is on one machine (`ws://127.0.0.1`).
* **Production secrets:** Windows Credential Manager/DPAPI on the desktop side; ACL-restricted
  `ProgramData\secrets` for services.

## 6. Phase plan (each phase: tests before and after, report, rollback)
| Phase | Scope | Acceptance |
|---|---|---|
| 1 | Infrastructure (A or B), encrypted backup/restore, **Ubuntu → Windows data migration** | Restore of an Ubuntu backup verified by row counts and strategy hashes; all existing tests pass on Windows CI |
| 2 | Tauri 2 shell + Rust supervisor + embedded existing console; WebView2 bootstrapper | App launches on Windows CI; services report healthy only through their health endpoints |
| 3 | MT5 Manager (headless sidecar, multiple terminals/profiles, start/stop terminal, latency) | DEMO identity verified; wrong login/server rejected (tests) |
| 4 | Worker lifecycle + execution-permission state machine + emergency-stop pending-order cancellation | Mandatory safety tests 1, 5–12 |
| 5 | AI provider gateway (Anthropic, Qwen, Ollama) + AI Studio | Gateway tests; AI cannot enable LIVE or raise limits (tests 3–4) |
| 6 | ASIE gaps (ranking, strategy-level shadow weights, MTF agreement) + Learner dashboard | Learner isolation (test 2); shadow-only proposals |
| 7 | Remaining desktop pages, Risk Centre, System Monitor, Backup & Recovery | UI tests; no fabricated figures |
| 8 | Installer (NSIS EXE + MSI), signed updater, upgrade with backup, uninstall | Clean-machine install, upgrade preserves data |
| 9 | Manuals, security architecture, release notes; acceptance on your VPS with the MT5 DEMO account | Section 15 acceptance criteria |

## 7. Ubuntu → Windows data migration (no volume copying)
1. On Ubuntu, run `./scripts/backup.sh`. It produces `pg_dump --format=custom` (PostgreSQL 16).
2. **Carry the same `RR_ENCRYPTION_KEY` and `RR_JWT_SECRET`.** Without the encryption key, stored broker
   passwords and connector bridge secrets cannot be decrypted.
3. On Windows, restore with `pg_restore` into an empty database of the same major version, then run
   `alembic upgrade head`.
4. Validate: table row counts on both sides, the strategy-hash preservation test, the latest trade IDs, and that
   login works.
5. Keep the Ubuntu deployment running (read-only for trading) until Windows acceptance passes.

## 8. Risk register
| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | VPS lacks nested virtualization | Option A impossible | Option B; decide before Phase 1 |
| R2 | MT5 terminal in a closed session (sign-out, reboot) | No market data/execution | Disconnect-not-sign-out guidance, auto-logon, supervisor alert; worker blocks entries when the connector is down (existing) |
| R3 | Encryption key lost during migration | Stored credentials unrecoverable | Migration checklist step 2; backup includes encrypted key escrow |
| R4 | No code-signing certificate | SmartScreen warnings; unsigned MSI | You provide an OV/EV certificate; Tauri updater signing key as a GitHub secret (you add it) |
| R5 | Scope (Tauri + Rust + supervisor + gateway) | Long delivery | Incremental phases, each releasable |
| R6 | AI output treated as instructions | Unsafe actions | Gateway returns data only; allowlisted supervisor; existing approval workflow |
| R7 | Accuracy/profit expectations | Financial loss | DEMO first; evidence gates (OOS, walk-forward, shadow) already enforced; no accuracy claims |
| R8 | WebView2 missing on Server 2022 | App won't start | Bundle the WebView2 bootstrapper in the installer |
| R9 | Live trading enabled accidentally | Real-money orders | LIVE locked by default; PC guard + engine approval + state machine; safety tests |

## 9. What I need from you
1. **Infrastructure:** run the check in section 4 on the VPS and send the output. That decides option A or B.
2. **Ubuntu data:** is there a production Ubuntu deployment with data to migrate, or does the Windows platform
   start fresh?
3. **Code signing:** do you have, or will you buy, a Windows code-signing certificate? Without one the app works
   but Windows shows warnings.
4. **AI keys:** which providers you'll use (Anthropic API key, Qwen/DashScope key, a local Ollama). Keys are
   entered in the app, never in chat.
