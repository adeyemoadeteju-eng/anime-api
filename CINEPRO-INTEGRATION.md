# CinePro Core integration notes

This repository's existing FastAPI service remains unchanged. CinePro Core is a separate Node.js/Docker application and should be deployed as a separate Render Web Service.

## Important separation

- Keep the existing `anime-api` service and its start command unchanged.
- Deploy CinePro Core from `https://github.com/cinepro-org/core` as its own Docker service.
- Do not point the existing Python service at CinePro until the CinePro service has a verified URL and a tested API contract.
- CinePro Core is licensed under PolyForm Noncommercial License 1.0.0. Review that license before using it for any commercial purpose.

## Render configuration for a separate CinePro service

Use the CinePro repository's Dockerfile and the Free instance for initial testing. Set:

| Variable | Value |
| --- | --- |
| `HOST` | `0.0.0.0` |
| `NODE_ENV` | `production` |
| `CACHE_TYPE` | `memory` |
| `CORS_ORIGIN` | The exact ZoroClone origin when known; use `*` only for temporary testing |
| `STREMIO_ADDON` | `false` unless Stremio compatibility is specifically needed |
| `MCP_ENABLED` | `false` unless MCP access is specifically needed |
| `INTERNAL_DEBUG` | `false` |
| `TMDB_API_KEY` | A real TMDB API key, entered as a Render secret |

Do not set `PORT` manually on Render; let Render supply it.

## Provider activation and safe integration

CinePro's upstream `src/server.ts` automatically discovers and registers provider modules from `./providers/`. The environment variables above do not disable that discovery. Do not expose a public deployment with provider scraping enabled unless every configured source is authorized for the intended use.

Before connecting CinePro to this API, verify the deployed service's health endpoint and documented API routes, then add an explicit server-side integration using only authorized sources. Do not expose secret keys in the frontend.

## Current status

This is an integration/deployment guide only. It does not modify the existing anime endpoints, activate providers, or claim that CinePro has been deployed.
