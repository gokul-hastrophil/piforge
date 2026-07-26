## What does this change?

<!-- One or two sentences. Why, not just what — the diff already shows what. -->

## How was this tested?

<!--
Be specific. CI covers syntax/validation checks and CodeQL, but this is a
tool that writes raw block devices — if this touches flash_device,
firstrun_gen.py, cancel/retry, or device detection, say how you verified
it against real hardware or at least a loop device. "It compiles" is not
enough for those paths.
-->

## Checklist

- [ ] Ran the local syntax checks from CONTRIBUTING.md before pushing
- [ ] No new runtime dependency added without discussion (this project is apt-only, zero pip)
- [ ] Updated README/CONTRIBUTING if behavior or setup steps changed
- [ ] If this touches device I/O or privilege handling, I've described how I tested it above
