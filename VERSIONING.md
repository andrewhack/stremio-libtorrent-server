# Component Versioning and Release Policy

This fork has three independent version references. They must not be treated as one release number.

## 1. Streaming Server release

Authoritative file: `SERVER_VERSION`

This version changes when the Stremio server image changes: core integration, runtime wrapper, server Dockerfile, server-side configuration behaviour, migration/update logic that changes the server image, or another server-runtime change.

`FORK_VERSION` is retained only as a compatibility alias for older installations and must always contain exactly the same value as `SERVER_VERSION`.

The WebAdmin server-update button compares the installed `/srv/app/SERVER_VERSION` with the remote `SERVER_VERSION`. A server update is activated transactionally with health validation and automatic rollback. WebAdmin and Pi-hole are not recreated.

## 2. WebAdmin release

Authoritative file: `webadmin/WEBADMIN_VERSION`

This version changes when the WebAdmin API, UI, logging, administration functions, version lifecycle layer, or WebAdmin container changes.

A WebAdmin release does not automatically rebuild the streaming server. The host-side activation command is:

```bash
git pull origin main && docker compose up -d --build --no-deps webadmin
```

This rebuilds only the `webadmin` service.

## 3. Core version

Source: the `stremiosrv` package version reported by `/health`.

The core version is informational. It identifies the upstream-derived server core included in the fork, but it is not the runtime update authority. Updates are always released through this fork.

## Release rules

- Server-only change: bump `SERVER_VERSION` and copy the same value to `FORK_VERSION`.
- WebAdmin-only change: bump `webadmin/WEBADMIN_VERSION` only.
- Change affecting both components: bump both independent versions.
- Upstream synchronization alone does not create a production release until the relevant fork version is deliberately bumped and the pull request is reviewed.
- `SERVER_VERSION`/`FORK_VERSION`, WebAdmin files, Compose files and release workflows are protected from automatic upstream replacement.

The Fork Integration Guard verifies these invariants on every pull request and push to `main`.
