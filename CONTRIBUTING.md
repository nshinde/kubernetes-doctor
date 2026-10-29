# Contributing

Contributions, diagnostic rules and synthetic reproduction cases are welcome. Please open an issue describing the expected symptoms and evidence before adding high-impact remediation behavior.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e . pytest
python -m pytest -q
./scripts/demo.sh
```

Keep examples synthetic, default to read-only behavior, include tests for each rule, and do not add any fix that could mutate production infrastructure without explicit policy and confirmation.
