## What this changes

A short description, and the issue it closes if there is one.

## Checklist

See [CONTRIBUTING.md](https://github.com/FernandoSLuz/agentfare/blob/main/CONTRIBUTING.md) for the reasons behind each item.

- [ ] `pytest` passes.
- [ ] New behaviour has tests, and any log fixtures are synthetic, with a `PRIVACY-CANARY-*` marker in every content field.
- [ ] No prompt text, real project name or real log content in code, tests, fixtures or docs.
- [ ] Amounts stay integers and prices stay `Decimal`.
- [ ] Prices come from the provider's page, with `source` and `checked`.
- [ ] Nothing outside `pricefetch.py` touches the network.
- [ ] A change to what a parser produces bumped `parsers.PARSERS_VERSION`.
- [ ] A model without a verified price stays out of dollar totals and is named in a warning.
- [ ] A schema change bumped `db.SCHEMA_VERSION` and has an upgrade test.
- [ ] `python tests/test_readme.py --write` was run and its diff reviewed.
- [ ] The docs that describe the change are updated: `README.md`, `README.pt-BR.md`, `docs/` and `CHANGELOG.md`.
- [ ] If the output of a command changed, `python scripts/make_assets.py` redrew the images.
