---
name: github-repo-finder
description: Find popular, recently active GitHub repositories in given languages that have open issues labeled for newcomers, from GitHub search and from curated lists (goodfirstissue.dev, up-for-grabs.net). Use as the first step when looking for open-source issues to work on, before checking how responsive the maintainers are. Requires a GitHub token.
---

# GitHub Repo Finder

GitHub's issue search cannot filter by the repository's stars, so the search starts from repositories: popular ones that are still active and have issues labeled for newcomers.

## Run

```
python scripts/github_repo_finder.py --language Python --language Java --language "C++" [--per-language 20]
python scripts/github_repo_finder.py --language Python --source search --source goodfirstissue --source upforgrabs
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

## Curated lists

GitHub's search counts only the exact labels "good first issue" and "help wanted", so projects that use "beginner", "E-easy" or "difficulty: easy" are never found by it. `--source` adds lists of newcomer-friendly projects kept by people:

- `goodfirstissue`: the ~800 repositories behind [goodfirstissue.dev](https://goodfirstissue.dev)
- `upforgrabs`: the ~1400 projects of [up-for-grabs.net](https://up-for-grabs.net), each naming its own newcomer label

Each listed repository is looked up with GitHub's GraphQL API, 40 per request, and held to the same requirements as the search: language, stars, last push, age, not archived or a fork, topics. Only those that pass get the slower count of open issues with a newcomer label: a common set (`NEWCOMER_LABELS` in the script) plus the project's own label from up-for-grabs. Then the best `--per-language` by `--sort` are kept. `source` on each repository says which source found it first. Sources run in the order given, and a repository already found is not looked up again.

The lists need a GitHub token; without one they are skipped. A list that cannot be downloaded is skipped with a message, and the other sources still run.

## Limits

- The search finds only repositories with the exact labels "good first issue" or "help wanted". Use the curated lists for the rest.
- The curated lists are slow: about 2 minutes for goodfirstissue and 3 for upforgrabs, though they cost few API points. A project not on any list, labeling its issues "beginner", is still missed.
- The search API allows 30 requests a minute. The skill pauses between requests, waits once if the limit is reached anyway, and otherwise returns what it found with `stopped` set.
- `totals` shows how many repositories matched each search, or each language and list. It is usually far more than `--per-language`, so an agent that remembers which repositories it already checked can get new ones on the next run with another `--sort`.
