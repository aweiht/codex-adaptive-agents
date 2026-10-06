# Codex Adaptive Agents documentation images

- `workflow-en.svg` and `workflow-zh.svg` illustrate the usage flow.
- `cli-check.svg` typesets selected fields from an actual installed 0.8.0
  source CLI result for `select --project` with no root-model argument. It was
  captured on 2026-09-30 on macOS arm64 / Python 3.14.6 after a temporary
  isolated install. It shows the local `xhigh` fallback role.
- `cli-check.json` contains those selected public fields and capture metadata.
  Private paths and unrelated fields are omitted. The installed package, home,
  and fallback-cache inputs were temporary local fixtures; no network or model
  task ran.

The CLI image is a command-output illustration, not a Codex app screenshot.
The full source CLI JSON was saved under the private work-package evidence.
`model_policy.json` remains in the runtime layout for receipt compatibility;
it has unrestricted metadata and does not determine current model identity or
delegation eligibility. No live model verification was run to create this image.

All images are original project documentation assets under the repository's
[Apache-2.0 license](../../LICENSE).
