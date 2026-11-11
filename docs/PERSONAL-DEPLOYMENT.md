# Cairn personal deployment — verified checkpoint (2026-11-11, Asia/Shanghai)

Status: NOT DEPLOYED. No production URL, HTTPS certificate, production secret, access gateway or full functional acceptance exists yet.

## Repository isolation
- Source: https://github.com/yes999zc/cairn-public
- Personal public fork: https://github.com/amwamw968/cairn-public
- Deployment branch: deploy/personal-full
- Source baseline: 231768005259937f3425280b2f1ae3f6ecb959e3
- Fork and branch were actually created and read back. Personal fork permissions include admin and push.
- No upstream write and no access/modification to factory_repo during this task.
- All upstream code, workflow definitions, LICENSE and NOTICE retained unchanged.
- LICENSE declares Apache License 2.0. NOTICE retains esp32-voice-entry attribution and MIT text. These files must remain in redistributed source/images; new Dockerfile copies both. No claim of a complete third-party license audit.

## Verified execution
See PERSONAL-DEPLOY-LOGS.md for actual logs.
- Python 3.12.14: editable install with dev dependencies succeeded; pip check found no broken requirements.
- Node 24.22.1: npm ci succeeded; TypeScript and Vite production build succeeded (1605 modules; JS 393.25 kB).
- RapidOCR initialized; a newly generated synthetic image recognized as CAIRN 12345 at about 0.996 confidence. This does not establish accuracy on real documents or Docker compatibility.
- Local TestClient: /, /stats, /api/tools, /api/workflows/default returned 200, twice across app recreation using the same SQLite file.
- Unauthorized workflow POST returned 401; exact-origin Authorization/Content-Type CORS preflight returned 200.
- SQLite reopened successfully (339968 bytes); this is a startup/reopen smoke check, not a backup/restore or volume restart test.
- Collection tests: 15 passed in 1.47 seconds.
- First full-suite failure: test_capture.py::test_lazy_import_survives_missing_pyobjc expects pyobjc in diagnostics, but Linux returns no AVFoundation. 38 tests passed before that first failure.
- Another isolated failure: test_crop_apply.py references docs/samples/hi-redbg.jpg, absent from the public snapshot.
- Full pytest was started but not completed: automatic approval review rejected an unexpected Microsoft telemetry connection because its payload and destination were not verified. The partial output must not be treated as a passing test suite. No workaround or remote CI rerun was used to bypass the rejection.
- Docker diagnostic: docker executable missing. No image build, Compose startup or Docker OCR test passed.

## Sites compatibility and preview
Sites accepts a static dist/index.html with assets, or a Cloudflare Worker fetch module.
- workflow-canvas is an ordinary React/Vite static build and meets the static output shape. Existing connection settings accept an external API origin, so no frontend rewrite is needed for that connection.
- API requests require a real HTTPS backend and exact CAIRN_WORKFLOW_CANVAS_ORIGINS. Vite server.proxy is development-only and does not become a production reverse proxy.
- Browser preview actually displayed Cairn Studio. It showed request failed (500), disconnected, unpublished because the preview API proxy was unavailable. This was UI rendering verification only, not integrated workflow acceptance. Temporary preview has been stopped.
- The learner UI is Python-rendered Jinja templates served by FastAPI, not part of the React static bundle. OCR needs Python native libraries; SQLite and scan files require a persistent filesystem. Sites Worker hosting cannot directly run this application unchanged.
- Therefore Sites is a viable candidate for Studio only. Full learner UI/API/OCR/SQLite stay on an independent Python environment. No platform substitution has been made, and no Site was published or production URL presented as a preview.

## Prepared staging files
- deploy/Dockerfile.personal: Python 3.12 official base, OpenCV system libraries, complete Python package, LICENSE and NOTICE, non-root user.
- deploy/compose.personal.yml: loopback-only 18100 port, named /data volume, SQLite and TTS cache under /data, required operator token and exact canvas origins, dropped capabilities.
- These files are preparation only, not Docker validated. Dependencies and base image still need reproducible pinning after a passing verification.
- Set CAIRN_WORKFLOW_ADMIN_TOKEN using a secure host secret/environment mechanism. Generate a fresh high-entropy value on the selected host; never place it in Git or build-time frontend variables.
- CAIRN_WORKFLOW_CANVAS_ORIGINS must be the final exact Studio HTTPS origin, not a wildcard.
- Do not enable cloud model endpoints until a provider/key and data-transfer scope are explicitly configured. No model credentials have been created or sent.

## Security and hosting work still required
1. Select/provide the independent Python/OCR host, persistent disk and an authorized deployment connection. Current scratch execution is not a durable hosting environment.
2. Configure the hostname, DNS and trusted HTTPS certificate at a gateway. Production configuration has not been applied.
3. Protect ALL learner pages, images, exports and APIs with owner-only identity access. The operator bearer token protects workflow writes/collections and is not whole-site authentication.
4. Validate cross-origin Studio API access with that gateway. Browser calls currently use no credentialed CORS; a gateway requiring cookies needs an explicit compatible integration. Do not remove authentication to fix this.
5. Keep MCP loopback/private until separately authenticated and required; compose does not expose it.
6. After Docker is available, validate image build, OCR, persistent volume restart, scan-image retention and backup/restore.
7. Resolve missing public test fixtures and Linux-specific expectations; investigate/disable unexpected telemetry before an authorized full test rerun.
8. End-to-end acceptance: workflow draft/save/validate/publish, collection import/export, upload/OCR/manual review, practice/history, database+image restore. None is claimed complete.

## Required owner input before production
- Independent backend host/environment and access connection (enter credentials only through secure settings, not chat).
- Backend hostname/domain and persistent storage allocation.
- Proposed audience: owner amwamw968 only. Confirm this after reviewing the preview checkpoint, before any publication.
