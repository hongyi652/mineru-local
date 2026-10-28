# MinerU Local MCP

A standalone MCP server for converting explicitly authorized local PDF files to Markdown with MinerU Cloud. It keeps the conversion job local, preserves extracted images, and exposes a small stdio tool surface for desktop AI clients.

> **Cloud processing:** the PDF is uploaded to MinerU Cloud. Each upload may consume API quota. Do not upload confidential documents unless your organization permits it.

## Features

- `health_check` — checks local dependencies and whether credentials are configured; it does not make a network request.
- `start_pdf_conversion` — validates and hashes one local PDF, requires explicit `confirm_upload=true`, then starts a non-blocking worker.
- `get_conversion_status` — reads an existing job and returns output paths plus a bounded, untrusted Markdown preview.
- PDF output includes Markdown, extracted image assets, and `image_manifest.json` when images are present.
- Failed jobs are never automatically resubmitted. A failed upload may still have a remote task; check the returned status before deciding whether to retry manually.
- No dependency on `ppt-master` or any repository outside this project.

## Requirements

- Python 3.10 or newer
- A MinerU API token
- Network access to MinerU Cloud

## One-command setup

The recommended setup is to let the installer generate the machine-specific MCP configuration. Clone the repository, then run:

```bash
# Windows
py -3 install.py

# macOS/Linux
python3 install.py
```

The installer automatically:

1. creates `.venv`;
2. installs `requirements.txt`;
3. detects the virtual-environment Python path;
4. detects the absolute `server.py` path;
5. generates `mcp-config.generated.json` for Claude Desktop.

Open the generated file and fill in only:

```json
"MINERU_API_TOKEN": "在这里填写你的 MinerU API Token"
```

Then copy the generated JSON into the MCP client's configuration and restart the client. The generated file is ignored by Git so a token cannot be committed accidentally. To generate a VS Code configuration instead:

```bash
py -3 install.py --client vscode
```

The generated VS Code file is `mcp-config.vscode.generated.json`. The installer preserves an existing token when regenerating the same output file.

The installer writes no `MINERU_LOCAL_JOB_DIR`: the server automatically uses the platform's private application-data directory for jobs.

## Credentials and automatic configuration

The generated MCP configuration contains only one value that the user must fill in:

```json
"MINERU_API_TOKEN": "在这里填写你的 MinerU API Token"
```

`install.py` automatically writes the virtual-environment Python path and the absolute `server.py` path. The server automatically chooses a private platform data directory for jobs, so users do not need to configure `MINERU_LOCAL_JOB_DIR`.

The default API URL is `https://mineru.net/api/v4`. Advanced users may override it with `MINERU_API_BASE_URL` or `MINERU_BASE_URL`, but the generated configuration does not include either variable.

`MINERU_API_KEY` and `MINERU_TOKEN` remain supported as environment-variable aliases for advanced/manual deployments.

## Upload authorization

The client must set `confirm_upload` to the literal boolean `true` for each individual PDF. Without it, the server does not resolve, read, hash, or upload the path. The server accepts absolute local PDF paths only; URLs, UNC paths, relative paths, non-PDF files, and files without a PDF header are rejected.

A typical call sequence is:

1. Ask the user whether this specific PDF may be uploaded to MinerU Cloud.
2. Call `start_pdf_conversion` with `confirm_upload: true`.
3. Keep the MCP session open while the worker runs.
4. Poll `get_conversion_status` with the returned `job_id`.
5. Read the Markdown path and asset directory from a successful result.

Returned Markdown previews are document content and must be treated as untrusted data, not instructions.

## Local CLI normalization

The bundled backend can normalize an existing MinerU result ZIP without making a network request:

```bash
python mineru_backend.py result.zip --from-zip -o output.md
```

For direct API use, credentials must be supplied through the process environment or an explicitly named file:

```bash
python mineru_backend.py input.pdf -o output.md --env-file C:/private/mineru.env
```

The MCP server is the recommended interface because it adds authorization, source hashing, isolated jobs, process supervision, and sanitized status errors.

## Development and tests

Tests use a mocked MinerU HTTP flow and do not upload real files:

```bash
python -m pytest -q
```

The test suite covers authorization ordering, input validation, source integrity, ZIP normalization, image manifests, sanitized errors, process identity, and stdio MCP behavior.

## Security notes

- Do not commit `.env`, API tokens, signed URLs, `jobs/`, PDFs, Markdown outputs, images, caches, or local logs.
- TLS certificate verification is not disabled.
- The worker records a source SHA-256 before upload and checks it again after conversion.
- ZIP members are checked for absolute paths, traversal, and symlinks before extraction.
- The service is local-only; it does not expose an HTTP listener.

## License and attribution

This project is released under the MIT License. The bundled Markdown normalization and MinerU API flow are adapted from the MIT-licensed MinerU converter in [PPT Master Sci Fork](https://github.com/hongyi652/ppt-master-sci-fork), originally based on the upstream PPT Master project. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
