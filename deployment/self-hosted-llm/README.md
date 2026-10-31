# GA401 Core API Model Bridge

Gives the core Firecrawl API (`firecrawl-api-1`, port `3002`) a model provider
for AI scrape formats (`json`, `summary`, `query`) without a cloud API key. It
relays OpenAI-compatible calls to GA401's `opencodex-proxy` on
`127.0.0.1:10100`, which serves them from the existing subscriptions.

```text
firecrawl-api-1 --OPENAI_BASE_URL--> 172.17.0.1:10180 (llm_bridge.py) --> 127.0.0.1:10100 opencodex-proxy
```

## Why TCP On docker0

The monitor judge and browser LLM bridges are private Unix sockets because the
monitor and browser API code was changed to call them over a socket. The core
API runs the unchanged upstream image, whose AI SDK speaks HTTP to
`OPENAI_BASE_URL` only. The bridge therefore listens on the docker0 address,
which every container reaches through `host.docker.internal`, and admits a call
only when every check passes:

| Check | Effect |
| --- | --- |
| Peer address | Must be a current address of `firecrawl-api-1` (looked up with `docker inspect`, refreshed after a recreate). Playwright, monitor, browser and session containers are refused. |
| Bridge key | `Authorization: Bearer <key>`, the key in `~/.local/state/firecrawl-core-llm/bridge-key` (0600). The core `.env` carries it as `OPENAI_API_KEY`. |
| Route | `POST /v1/responses` or `/v1/chat/completions`; nothing else. |
| Body | `application/json` with a `Content-Length` up to 16 MiB. A page script cannot send this cross-origin without a CORS preflight, which the bridge refuses. |
| Model | Only `FIRECRAWL_CORE_LLM_MODELS` (default `gpt-6.1-terra`). Any other model fails visibly; there is no fallback. |

The core API sends no reasoning effort, and opencodex has no `<model>--<effort>` id for
ChatGPT models, so the bridge adds `FIRECRAWL_CORE_LLM_EFFORT` (default `low`) as
`reasoning.effort` (Responses) or `reasoning_effort` (Chat Completions) when the
caller set none. Without it opencodex uses `medium`. The `--fast` (priority tier)
selector is not used: on 2026-10-31 it was not faster for extraction and costs more
subscription quota.

The optional opencodex token (`~/.opencodex/service-api-token`) stays on the
host and is added only on the upstream call. Logs record status, model, route
and duration, never prompts, page content or credentials. At most four calls run
at once.

## Core Configuration

The model name is set once through `MODEL_NAME`, which the core API applies to
every OpenAI-provider call. `/home/marck/github/firecrawl/.env` gains:

```text
OPENAI_API_KEY=<bridge key>
OPENAI_BASE_URL=http://host.docker.internal:10180/v1
MODEL_NAME=gpt-6.1-terra
```

Only the `api` service is recreated, with the same Compose files and image the
running container uses (its `com.docker.compose.project.config_files` label), so
the image-only security refresh keeps its pinned image:

```text
docker compose --project-name firecrawl -f <each config file> up -d --no-deps --no-build --pull never --force-recreate api
```

`query` with `mode: directQuote` uses a hosted Fireworks model upstream and stays
unavailable. Freeform `query`, `summary` and `json` use the OpenAI provider.

## Install

```text
release=~/services/firecrawl-security/releases/core-llm-bridge
install -m 0644 llm_bridge.py "$release/"
install -m 0644 firecrawl-core-llm.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now firecrawl-core-llm.service
```

Copy files from `git cat-file blob`, not from a Windows checkout, so the
deployed bytes match the commit.

## Checks

```text
python3 -B -m unittest discover -s deployment/self-hosted-llm -p 'test_*.py'
```
