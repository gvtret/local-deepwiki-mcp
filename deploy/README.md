# Local DeepWiki deploy layer

App root on server: `/opt/local-deepwiki` (owner `deploy`).  
Does **not** touch `/opt/doc-rag-mcp` or `doc-rag-mcp.service`.

## Components

| Path | Role |
|------|------|
| `POST/GET /mcp` | Streamable HTTP MCP (JSON-RPC + SSE) |
| `GET /health` | Liveness |
| `GET /ui` | Multi-repo Hub (add / reindex / open wiki) |
| `/r/<name>/` | Proxy to per-repo wiki UI |
| `deploy/scripts/run_mcp_stdio.sh` | stdio MCP for Cursor |

## Install (server)

```bash
sudo mkdir -p /opt/local-deepwiki
sudo chown deploy:deploy /opt/local-deepwiki
rsync -az --exclude .venv --exclude data --exclude .git \
  ./ deploy@192.168.1.165:/opt/local-deepwiki/
ssh deploy@192.168.1.165
cd /opt/local-deepwiki
cp -n deploy/config.yaml ./config.yaml
# CPU-only torch (no nvidia-* wheels — host has no GPU):
bash deploy/scripts/install_deps_cpu.sh
# LLM: Omniroute (OpenAI-compatible) — copy env with OPENAI_API_KEY
cp -n deploy/env.example .env
# Edit .env if needed. Default base_url in config.yaml: http://omniroute.misc-server/v1
sudo cp deploy/local-deepwiki.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now local-deepwiki
# Caddy: append deploy/Caddyfile.snippet to /etc/caddy/Caddyfile, then:
sudo systemctl reload caddy
```

## Cursor — HTTP

```json
{
  "mcpServers": {
    "local-deepwiki": {
      "transport": "streamableHttp",
      "url": "http://192.168.1.165:5555/mcp"
    }
  }
}
```

Drop-in: `http://192.168.1.165:5555/ui/mcp/cursor.json`

## Cursor — stdio (on the server / checkout machine)

```json
{
  "mcpServers": {
    "local-deepwiki-stdio": {
      "command": "/opt/local-deepwiki/deploy/scripts/run_mcp_stdio.sh",
      "args": []
    }
  }
}
```

Drop-in: `/ui/mcp/cursor-stdio.json`

## Cursor — stdio over SSH

```json
{
  "mcpServers": {
    "local-deepwiki-stdio-ssh": {
      "command": "ssh",
      "args": [
        "deploy@192.168.1.165",
        "/opt/local-deepwiki/deploy/scripts/run_mcp_stdio.sh"
      ]
    }
  }
}
```

Drop-in: `/ui/mcp/cursor-stdio-ssh.json`

## Add repo: Git URL or local folder

Hub UI (`/ui`) supports two sources:

1. **Git URL** — shallow clone into `data/repos/<name>/`, then index.
2. **Local folder** — absolute path on the **server** (visible to the `deploy` user):
   - default: copy into `data/repos/<name>/` (safe Delete);
   - **Index in place**: index the path itself; Delete only unregisters the repo.
   - Reindex for copied locals re-syncs from the original path first.

Allowlist (optional env `LOCAL_DEEPWIKI_LOCAL_ROOTS`): paths must resolve under `/home`, `/opt`, `/srv`, `/var/lib`, `$HOME`, or `LOCAL_DEEPWIKI_ROOT` by default.

```bash
curl -sS -X POST http://127.0.0.1:5555/ui/api/repos \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-src","local_path":"/home/deploy/src/my-src"}'
```

## Verify

```bash
curl -sS http://127.0.0.1:5555/health
curl -sS -X POST http://127.0.0.1:5555/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
systemctl is-active doc-rag-mcp   # must stay active
```
