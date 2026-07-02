# LMAO project rules

## E2E flash verification

Run the following as final verification before marking any feature complete or submitting a PR:

```bash
bazel test //tests:test_cardputer_e2e --test_output=all
```

Requires a physical Cardputer connected. The test auto-skips if no hardware is detected.
