# Contributing to Capalith

Thanks for looking. Capalith is a young project (alpha, Linux x86_64 with Python 3.11 is the one supported platform), so the most useful things you can send are clear bug reports, small fixes and test cases.

## Before you start

- **A bug or an idea:** open an issue first for anything bigger than a typo or a one-line fix. It saves you work if the change doesn't fit.
- **A security problem:** don't open an issue. Follow [SECURITY.md](SECURITY.md).
- **Behaviour in the README is a promise.** If your change alters what a command prints, what a status word means or what a setting does, the README, `docs/` and the [CHANGELOG](CHANGELOG.md) change in the same pull request.

## Set up

You need Python 3.11 and SQLite with FTS5. From a clone of the repository:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
```

`requirements.txt` pins every dependency by hash and is the file CI installs from, so use it when you work on the code.

The search model is only needed to try the program by hand, not to run the tests: `python -m capalith.semantic provision` downloads it once (see the README).

## Run the tests

This is the exact command CI runs, and a pull request needs it to pass:

```bash
PYTHONWARNINGS=error python -m unittest discover -s tests -v
```

`PYTHONWARNINGS=error` turns every warning into a failure, so a new warning is a test failure.

## What a change needs

1. **A test** for any change in behaviour: it fails without your change and passes with it. Put it in `tests/`, in the style of the neighbouring tests (standard `unittest`, no new test framework). A pull request that only changes documentation needs no test; say so in the description.
2. **The whole suite green** with the command above.
3. **Docs and changelog** updated where behaviour changes (see above). Write for a person who has never seen the project; say what you measured and what you did not check.
4. **One concern per pull request**, with a description that says what changed and why. Fill in the pull request template.

### Dependencies and CI actions

- Every dependency is pinned with a hash. Don't add a runtime dependency without opening an issue first: the project records each one's licence and source in `DEPENDENCIES.json` and `THIRD_PARTY_NOTICES.md`, and a new one needs a licence check.
- If you change a pin in `requirements.txt`, regenerate the other platforms' files with `python tools/lock_hashes.py`.
- GitHub Actions in `.github/workflows` are pinned to a full commit with the version in a comment; `python tools/check_action_pins.py` checks it.

## What won't be accepted yet

- **Support claims for another platform.** macOS, Windows (through WSL2) and newer Pythons run in CI so the results can be read, but nothing is listed as supported until its job passes. Fixes that move a job toward passing are welcome; changing the README's "supported" line is the maintainers' step.
- **New network use.** Indexing and search of local folders run locally. The network is used only where the README says: the one-time model download (`capalith-model provision`), fetching a configured Git branch or tag for a Git source, and the optional AI ranking, which is off unless the user turns it on. A change that adds another use needs an issue first and is unlikely to be accepted.
- **Changes that make Capalith install, activate or edit a host's skills.** Capalith finds and ranks skills and says where to read them; the host keeps control of loading. It reads the settings file and never writes it.

## Pull requests

Fork the repository, make a branch, push it, and open a pull request against `main`. A maintainer will review it and reply in the pull request.

By contributing you agree that your contribution is licensed under the project's [MIT License](LICENSE). Please follow the [code of conduct](CODE_OF_CONDUCT.md).
