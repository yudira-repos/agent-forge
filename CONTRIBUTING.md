# Contributing to AgentForge

Thank you for your interest in contributing. AgentForge is an open-source project and we welcome contributions of all kinds.

## Ways to Contribute

- **Bug reports** — Open an issue with steps to reproduce
- **Feature requests** — Open an issue describing the use case first
- **Compliance profiles** — Add SOC 2, HIPAA, GDPR, FedRAMP, ISO 27001 profiles to `agentforge/governance/profiles.py`
- **Runtime adapters** — Add support for new LLM providers in `agentforge/runtime/adapters/`
- **Registry backends** — Add PostgreSQL, Redis, or DynamoDB backends
- **Documentation** — Improve docs, add examples, write tutorials

## Development Setup

```bash
git clone https://github.com/agentforge-oss/agentforge
cd agentforge
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
```

## Pull Request Process

1. Fork the repo and create a branch from `main`: `git checkout -b feat/my-feature`
2. Write tests for any new functionality — coverage must stay above 80%
3. Run `ruff check agentforge tests` and fix any lint issues
4. Update `CHANGELOG.md` under `[Unreleased]`
5. Open a PR with a clear description of what and why

## Commit Style

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add Redis registry backend
fix: trust chain cycle guard infinite loop
docs: add HIPAA profile usage example
test: add RBAC inheritance edge cases
chore: bump anthropic to 0.32
```

## Compliance Profile Contributions

Adding a new compliance profile is one of the highest-value contributions:

```python
# agentforge/governance/profiles.py
class FedRAMPProfile(ComplianceProfile):
    name = "fedramp"

    @classmethod
    def apply(cls, engine: PolicyEngine) -> PolicyEngine:
        # Add your rules here
        ...
        return engine
```

Please include:
- A reference to the specific control requirement (e.g. "FedRAMP AC-2")
- At least two tests in `tests/test_governance.py`
- A brief description in `docs/governance.md`

## Code of Conduct

Be kind, be constructive, assume good intent.
Harassment of any kind will not be tolerated.

## License

By contributing, you agree your contributions will be licensed under Apache 2.0.
