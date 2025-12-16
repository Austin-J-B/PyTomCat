# Security Policy

## Reporting a Vulnerability
- Email critical or suspected vulnerabilities to [utacampuscats@gmail.com](mailto:utacampuscats@gmail.com).
- Please include reproduction steps, logs, and the affected endpoints or commands.
- Do not open public GitHub issues for security problems.

## Scope
- The Discord bot runtime, including all commands and event handlers.
- The embedded web API (aiohttp) and its `/api/*` endpoints.
- File handling and logging paths under the `logs/` and `cache/` directories.

## Supported Versions
- The latest `main` branch releases receive security updates. Older revisions may not be patched.

## Handling Process
- Reports are acknowledged within 72 hours.
- Fixes are triaged by impact and deployed as soon as practical.
- Credit is provided upon request after the issue is resolved.

## Dependency Security
- Core web dependencies such as `aiohttp` and image libraries like `Pillow` are monitored for upstream CVEs.
- Updates are applied promptly when compatible with the codebase, and downstream services are retested after upgrades.
