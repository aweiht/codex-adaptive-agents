# Codex Adaptive Agents documentation images

- `workflow-en.svg` and `workflow-zh.svg` illustrate the usage flow.
- `cli-check.svg` typesets selected fields from an actual installed 0.9.0
  source CLI result for `select --task-kind browser --explain`. It was captured
  on 2026-10-28 on macOS arm64 / Python 3.14.6 after an isolated install. It
  shows the dedicated Terra `xhigh` route using a synthetic public catalogue.
- `cli-check.json` contains those selected public fields and capture metadata.
  Private paths and unrelated fields are omitted. The installed package, home,
  and public catalogue were isolated local fixtures; no network or model task
  ran.

The CLI image is a command-output illustration, not a Codex app screenshot.
The full source CLI JSON was saved under the private work-package evidence.
The image demonstrates routing against a synthetic public catalogue. It does
not establish live model availability, provider access, or GUI execution.

All images are original project documentation assets under the repository's
[Apache-2.0 license](../../LICENSE).
