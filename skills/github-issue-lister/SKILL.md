---
name: github-issue-lister
description: List the open, unassigned issues that GitHub repositories label for newcomers ("good first issue", "E-easy", "beginner" and similar), one flat list ready for rule-filter and profile-matcher. Use after repo-health-checker, on the healthy repositories. Requires a GitHub token.
---

# GitHub Issue Lister

## Run

```
python scripts/github_issue_lister.py owner/name [owner/name ...]
python scripts/github_issue_lister.py < health.json
```

Input is repository names as arguments, or from stdin the result of `repo-health-checker` (its `healthy` repositories, or the statuses given with `--include`), `github-repo-finder` (`repos`) or `rule-filter` (`kept`), or a JSON array of names or objects with `full_name`.

Writes a JSON object with `issues`, a flat list, and `repos`, what happened in each repository. The format is defined in [schema.json](schema.json). Progress goes to stderr. To pass the issues on, use the `issues` list, for example `profile-matcher --fields title,repo,labels,body`.

The token is taken from `GITHUB_TOKEN`, or from `gh auth token`.

## Which labels count

Every project names its newcomer label differently: "good first issue", "Good first issue", "good first issue for humans", "E-easy", "beginner", "difficulty: easy". So the skill reads each repository's own labels and picks those whose name *starts* with a known pattern (`BEGINNER_LABEL` in the script). Starting, not containing, so "I-lang-easy-decision" and "D-newcomer-roadblock" are not picked.

- `--help-wanted` adds "help wanted" labels. Those issues are wanted, not necessarily easy.
- `--label` adds a label name used by one project, matched exactly.

## Which issues are kept

- open, with no assignee, not locked, and not a pull request
- opened in the last year (`--max-age-days`). A newcomer issue nobody solved in years is rarely easy.
- the newest 10 per repository (`--per-repo`)

An issue with no assignee may still be taken: someone may have said "I'm working on it" in a comment or opened a pull request. Use `issue-reader` on the best issues to check.

## Limits

- One request for the labels and one per newcomer label, per repository.
- The issue body is cut to 4000 characters, and HTML comments left by issue templates are removed.
