---
name: issue-reader
description: Read the comments and linked pull requests of GitHub issues and sort them into available, taken, closed and unknown, adding the full discussion text. Use after ranking, on the best issues only, so nobody starts work on an issue someone else is already doing, and so the next scoring step sees the whole discussion. Requires a GitHub token.
---

# Issue Reader

An issue with no assignee is often taken anyway: someone opened a pull request or wrote "Can I work on this?". On real newcomer issues, most turned out to be taken this way. This skill reads each issue's history and says which ones are really free.

## Run

```
python scripts/issue_reader.py https://github.com/owner/name/issues/12 [...]
python scripts/issue_reader.py [--limit 20] < issues.json
```

Input is issue links as arguments, or from stdin a JSON array, or the object written by `github-issue-lister` (`issues`), `rule-filter` (`kept`) or `profile-matcher` (`scored`). The issue is found from `url` or `key` (`owner/name#12`), on the entry or its `item`. Each entry is returned unchanged, with a `thread` field added, under `available`, `taken`, `closed` or `unknown`. Output is defined in [schema.json](schema.json). Progress goes to stderr.

The token is taken from `GITHUB_TOKEN`, or from `gh auth token`. It is required, because GitHub's GraphQL API, used to find the pull requests that close an issue, does not work without one.

## How an issue is judged

In this order:

1. `closed` if it is closed.
2. `taken` if it is assigned.
3. `taken` if an open pull request will close it. GitHub records this when the PR says "Fixes #12" or is linked in the sidebar. A PR that only mentions the issue does not count, because large issues are often done in parts by many PRs, and those PRs are listed in `notes` instead.
4. `taken` if someone outside the team asked for it ("I'd like to work on this", "Can I take this?", "/assign") in the last 30 days (`--claim-days`) and did not give it back. An older request with no PR has lapsed and is listed in `notes`.
5. `unknown` if a merged PR should have closed it but the issue is still open: it may already be fixed.
6. `available` otherwise.

## What is added

- `reason` for the status, and `notes`, such as lapsed claims or earlier PRs that were closed without merging.
- `linked_prs` and `claims`, the evidence.
- `last_maintainer_comment_at`, to see whether anyone on the team is still around.
- `text`: the issue and its comments by people, oldest first, cut to 8000 characters, for the next scoring step.

## Limits

- Three or more requests per issue, so keep `--limit` small: it is meant for the best issues, not all of them.
- Claims are recognized by common English phrases. A claim in other words, or in another language, is missed, and so is a claim made in a chat outside GitHub.
