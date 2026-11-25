# Security policy

## Reporting a vulnerability

Please report it privately; don't open a public issue or pull request.

Use GitHub's private reporting: open the repository's **Security and quality** tab, choose **Report a vulnerability**, and fill in the form. The report goes privately to the maintainers; it is not public.

Include what you found, how to reproduce it, the Capalith version (`capalith --version`) and what an attacker could do with it. Please don't include real API keys or private skill contents.

## What to expect

This is a small project run by a few people, so these are intentions and not guarantees:

- we aim to acknowledge a report within a week;
- we tell you whether we think it is a vulnerability and what we plan to do;
- we credit you in the fix's release notes if you want that.

Please give us reasonable time to fix a problem before you describe it publicly.

## Supported versions

Capalith is alpha software. Only the latest release gets security fixes.

## What counts

Capalith runs on your machine, reads skill folders you point it at, and keeps a local catalog and settings. Reports about, for example, reading or writing outside the folders it is given, a catalog or settings file that another user can tamper with, text from a skill acting as an instruction to the program, or a secret ending up in an output or a log are in scope. Problems in a dependency belong with that dependency's project; tell us too if Capalith's pinned version is affected.
