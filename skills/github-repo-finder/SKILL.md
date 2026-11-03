---
name: github-repo-finder
description: Find popular, recently active GitHub repositories in given languages that have open "good first issue" (or "help wanted") issues. Use as the first step when looking for open-source issues to work on, before checking how responsive the maintainers are. Requires a GitHub token.
---

# GitHub Repo Finder

GitHub's issue search cannot filter by the repository's stars, so the search starts from repositories: popular ones that are still active and have issues labeled for newcomers.

## Run

```
python scripts/github_repo_finder.py --language Python --language Java --language "C++" [--per-language 20]
```

Writes a JSON object to stdout with `repos`, `totals` and `stopped`. The format is defined in [schema.json](schema.json). Progress goes to stderr. The output can be piped straight into `repo-health-checker`.

The token is taken from `GITHUB_TOKEN`, or from `gh auth token`.

## What is searched

One search per language, so a language with many projects does not crowd out the rest. A repository must:

- have at least 1000 stars (`--min-stars`, and optionally `--max-stars` to skip the largest projects, which get many newcomers)
- have been pushed to in the last 30 days (`--pushed-days`)
- be at least a year old (`--min-age-days`). New repositories with thousands of stars are usually riding a wave of attention, not a stable place for a first contribution.
- have at least one open issue labeled "good first issue" (`--min-issues`). With `--help-wanted`, repositories with "help wanted" issues are searched too.
- not be archived or a fork
- have every `--topic` given

Results are newest activity first (`--sort`). A repository found by two searches is returned once.

## Limits

- GitHub counts only the exact labels "good first issue" and "help wanted". Projects that use other names, such as "beginner" or "easy", are not found here.
- The search API allows 30 requests a minute. The skill pauses between requests, waits once if the limit is reached anyway, and otherwise returns what it found with `stopped` set.
- `totals` shows how many repositories matched each search. It is usually far more than `--per-language`, so an agent that remembers which repositories it already checked can get new ones on the next run with another `--sort`.
