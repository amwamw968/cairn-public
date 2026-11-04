# Cairn Studio · workflow canvas

Independent authoring UI for Cairn's published learning workflows. It is a separate React + React Flow app; the parent-facing Cairn templates never link to it.

## Local development

```sh
npm install
CAIRN_API_TARGET=http://127.0.0.1:18100 npm run dev
```

Open `http://localhost:5173`. The Vite dev server proxies `/api` to `CAIRN_API_TARGET` (default `http://127.0.0.1:18100`). Enter the backend's `CAIRN_WORKFLOW_ADMIN_TOKEN` in **连接设置**. The token is kept in `sessionStorage` for the current tab only. The API origin preference is kept in `localStorage`.

Use **English / 中文** in the header to switch the authoring UI language. The preference stays in this browser; switching it does not reload the workflow or change the draft. Author-supplied workflow titles remain as written. The canvas is currently designed for desktop-width screens (minimum 1050 px).

## Question collections

The **Collections** button opens an operator-only library for the current learner. Create a collection with a subject, license, author and source, select reviewed questions, and publish a read-only version. A version can be downloaded as a text-only ZIP and imported as a separate local collection. The ZIP contains question and answer text plus provenance, not images or learning records; the text may still contain personal information and must be reviewed before sharing.

**Add current collection to practice** acts on the current saved draft, adding only questions without an existing learning record; existing records and archived items are preserved. It does not execute a collection workflow node or promise a fixed version. Importing a package alone does not add questions to practice. Deleting a question revokes and erases locally stored version snapshots containing that question when no active workflow or practice copy depends on the version; previously downloaded ZIPs remain wherever they were saved.

The **Add to collection** workflow node is an output step placed after human review. It adds confirmed questions from a scan to the selected collection's draft, without changing existing published collection versions. A subject mismatch fails visibly instead of silently skipping questions. The collection belongs to the learner selected when publishing: if another learner is active, creating a scan with this workflow is refused before the image is saved. Deleting a collection is blocked while an active workflow, an unfinished scan, or a practice copy still refers to it; an old version cannot be reactivated after its collection is deleted.

The separate **Collection workflow** starts with a **Collection version** node and can enable a review strategy, guided help, and text recall. Publish it in Studio; the reader can then use **Settings → Collection practice** to add independent copies from that immutable version to the regular practice queue. The original question may also be in that queue. Starting the same version again reuses its copies and preserves review history; the strategy of an already reviewed item cannot be silently changed. Version history can deactivate the collection workflow. A version with practice copies protects all of its source questions from deletion until those copies are handled. This is not a collection-specific practice session or a filter node.

The operator API requires the same bearer token as workflow writes. Package import uses `POST /api/collections/import` with a bounded raw `application/zip` body; exports are available from each published version. Collections and snapshots are included in the learner's JSON learning backup, but workflow definitions and scan images are not.

## Build and static hosting

```sh
npm run build
```

Serve `dist/` from any static host under its own operator URL. Set the Cairn service's `CAIRN_WORKFLOW_CANVAS_ORIGINS` to the exact origin(s) serving this app, and configure `CAIRN_WORKFLOW_ADMIN_TOKEN` outside the repository. The UI asks for the Cairn API origin in **连接设置**. Do not expose the canvas through the reader app's navigation.

For a separate 18101 Studio serving a 18100 API on the same host, build with `VITE_CAIRN_API_PORT=18100 npm run build`, then use `deploy/Dockerfile.studio`. The operator token is configured in the reader container environment, not in the image or browser permanent storage. Plain HTTP should only be used on a trusted network. The source and workflow APIs require the configured token for writes and collection operations.

The existing `cairn-deps:local` image's OpenCV wheel requires system libraries that are not present in its Python slim base. After building the app image, build `deploy/Dockerfile.ocr-runtime` with `--build-arg CAIRN_APP_IMAGE=<app-image>` and deploy that resulting image as the reader; the layer installs the missing Debian libraries and checks that RapidOCR initializes. This extra layer needs Debian package network access, and the package versions are not pinned to a snapshot.

Text tutoring and recall use an OpenAI-compatible chat endpoint configured with `CAIRN_TUTOR_LLM_URL`, `CAIRN_TUTOR_LLM_MODEL`, and either `CAIRN_TUTOR_LLM_API_KEY` or `CAIRN_TUTOR_LLM_KEY_FILE`. An explicitly supplied key-file argument takes precedence over the environment key; otherwise the environment key takes precedence over the configured key file. `CAIRN_TUTOR_LLM_THINKING=disabled` is an opt-in for providers that support this parameter; leave it unset for providers that do not. If no model is configured, tutoring falls back to fixed prompts and recall reports that feedback is unavailable. Configuring a cloud endpoint sends question and response text to that provider; a model service must be configured explicitly.

`scripts/eval_recap_model.py` exercises a small synthetic benchmark against an explicitly selected loopback service or, with `--allow-deepseek`, DeepSeek's official HTTPS endpoint. Supply an API key through the named environment variable with `--key-env`; the script does not write it to a file. These examples are regression aids, not an assessment of teaching effectiveness or comprehensive prompt-injection resistance.

## Contract

The canvas calls `/api/tools`, `/api/workflows/*`, and the collection APIs. It edits graph drafts, requests validation/previews, and asks the backend to publish with the revision just saved; a concurrent edit produces a conflict instead of publishing a different draft. The Cairn service is the only writer of immutable runtime versions. MVP graphs must be one connected linear path; branches, loops, unknown node IDs, unknown parameters, and arbitrary code are rejected. Connection settings are applied only after an explicit successful connection, and switching or reloading warns before replacing unsaved canvas edits.
