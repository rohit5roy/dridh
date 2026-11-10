## Summary

## Verification

- [ ] `uv run ruff check src tests`
- [ ] `uv run pytest -m "not live and not docker" -q`
- [ ] Plugin tests (`npm test` in `dridh/opencode/plugins/dridh-bridge`) if the plugin changed
