# Handoff — local-deepwiki

Living progress log. Last entry = current state.

## 2026-07-26 19:55 — Post-CSRF fix, continue polish

**Done:**
- Deployed UrbanDiver/local-deepwiki-mcp + Hub on `deploy@192.168.1.165:/opt/local-deepwiki`
- HTTP MCP `:5555` (`/mcp`, `/health`, `/ui`), Caddy `deepwiki.misc-server`, doc-rag untouched
- LLM: Omniroute `http://omniroute.misc-server/v1` model `auto/best-coding`; embeddings local `all-MiniLM-L6-v2`
- Fixed `Config.load()` to honor `LOCAL_DEEPWIKI_ROOT` / cwd (was defaulting to gpt-4o → fake auth errors)
- Wiki proxy: rewrite absolute `/wiki|/api|/chat` under `/r/{name}/`, stream SSE (chat), CSRF accepts public Origin via `X-Forwarded-*` + `LOCAL_DEEPWIKI_ALLOWED_ORIGINS`
- Empty LLM cache responses no longer cached/returned (chat was blank)
- `google/benchmark` indexed **ready**; chat + codemap verified
- `spodes-rs` wiki mostly built; status was **error** on late Omniroute overload during optional codemap — reindex triggered; Hub soft-ready when `index.md` exists

**State:**
- Unit `local-deepwiki.service` active; `doc-rag-mcp` active
- Key in `/opt/local-deepwiki/.env` (chmod 600), not in git
- Custom non-AVX `lancedb`/`pylance` wheels on host (QEMU CPU)
- Workspace: `/home/trgv/local-deepwiki` (upstream + `deploy/`)

**Next:**
1. Confirm `spodes-rs` → ready (or soft-ready) after reindex; smoke chat on `/r/spodes-rs/chat`
2. Sync latest `deploy/http/repos.py` soft-ready patch to server if reindex still running on old code
3. Optional: commit deploy-layer fixes (no secrets); Cursor MCP drop-in already at `/ui/mcp/cursor.json`

**Notes:**
- Never `pkill -f "deepwiki serve"` inside an ssh one-liner that contains that string — kills the shell; use `fuser -k 5600/tcp`
- Overview in Codemap is slow (~1 min LLM); prefer Generate on a topic
- Omniroute `auto/best-fast` often 503 (combo pool); prefer `auto/best-coding`

## 2026-07-26 20:00 — spodes ready + soft-fail + E2E

**Done:**
- Stopped wasteful `--full-rebuild` of spodes-rs; marked **ready** (wiki already had index + 110 file docs)
- Deployed Hub soft-ready: if `deepwiki update` fails but `.deepwiki/index.md` exists → status `ready` with warnings
- E2E: wiki/arch/chat (CSRF Origin OK), MCP 65 tools, doc-rag still healthy
- spodes-rs chat smoke via `/r/spodes-rs/api/chat`

**State:** both repos **ready**; Hub + CSRF + streaming chat on server

**Next:** optional git commit of deploy-layer fixes (ask user); Cursor HTTP MCP already at `http://192.168.1.165:5555/ui/mcp/cursor.json`

## 2026-07-26 20:10 — Fork + commit

**Done:**
- Fork: https://github.com/gvtret/local-deepwiki-mcp (upstream UrbanDiver/local-deepwiki-mcp)
- Commit `9661cce` on `main`: Hub deploy layer, Config.load ROOT, proxy SSE/CSRF, empty LLM cache harden, chat UI errors
- Workspace `/home/trgv/local-deepwiki` re-synced to the fork clone (`origin`=fork, `upstream`=UrbanDiver)

**State:** fork pushed; server `/opt/local-deepwiki` unchanged (still running)

**Next:** none required; optional PR upstream or sync server from fork
