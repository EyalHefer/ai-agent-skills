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
3. `taken` if an open pull request will close it. GitHub records this when the PR says "Fixes #12" or is linked in the sidebar.
4. `taken` if an open pull request mentioned it in the last 30 days (`--claim-days`), even without "Fixes #12": many fixes are written that way. Not when 3 or more PRs mention the issue, since it is then being done in parts; those PRs are listed in `notes` instead.
5. `taken` if someone pushed a commit that mentions it to their fork in the last 30 days: the work has started, even with no PR or comment yet.
6. `taken` if someone outside the team asked for it ("I'd like to work on this", "Can I give this a try?", "I've started working on this", "I'll open a PR", "/assign") in the last 30 days and did not give it back. An older request with no PR has lapsed and is listed in `notes`.
7. `unknown` if a merged PR should have closed it but the issue is still open: it may already be fixed.
8. `available` otherwise. When 3 or more outside people asked for it or about it ("is this still open?", "any update?", lapsed claims too), a `crowded` note says to expect competition.

## What is added

- `reason` for the status, and `notes`, such as lapsed claims or earlier PRs that were closed without merging.
- `linked_prs`, `claims` and `fork_commits`, the evidence.
- `interested`: the outside people who asked for the issue or about it, other than its author.
- `last_maintainer_comment_at`, to see whether anyone on the team is still around.
- `text`: the issue and its comments by people, oldest first, cut to 8000 characters, for the next scoring step.

## Limits

- Three or more requests per issue, so keep `--limit` small: it is meant for the best issues, not all of them.
- Claims and interest are recognized by common English phrases. A claim in other words, or in another language, is missed, and so is a claim made in a chat outside GitHub.
