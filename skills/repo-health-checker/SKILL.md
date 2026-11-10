---
name: repo-health-checker
description: Measure how quickly the maintainers of GitHub repositories answer and merge outside contributions, and sort the repositories into healthy, unhealthy, inactive and unknown. Also quotes what their contributing guide says about AI and CLAs. Use before picking issues to work on, so a first contribution does not wait months for a review. Requires a GitHub token.
---

# Repo Health Checker

Stars and recent commits show that a project is popular and alive, not that anyone will look at a newcomer's pull request. This skill measures that from the project's own recent history.

## Run

```
python scripts/repo_health_checker.py owner/name [owner/name ...]
python scripts/repo_health_checker.py < repos.json
```

Input is repository names as arguments, or from stdin a JSON array of names or of objects with a `full_name` field (`--repo-field`), or the object written by `github-repo-finder` (`repos`) or `rule-filter` (`kept`). Each entry is returned unchanged, with a `health` field added, under `healthy`, `unhealthy`, `inactive` or `unknown`. Output is defined in [schema.json](schema.json). Progress goes to stderr.

The token is taken from `GITHUB_TOKEN`, or from `gh auth token`.

## What is measured

The latest outside issues and PRs from the last 90 days (`--days`), up to 15 of each (`--sample`). Items opened by maintainers or bots are skipped, since they are not waiting for anyone, and so are items younger than 7 days (`--min-age-days`), which have not had a fair chance yet. Anyone who opened 5 or more items in the window is treated as staff too: company projects often hide their employees' membership, and those employees' PRs, merged by colleagues within hours, would make the project look fast to newcomers.

- **First answer.** A comment or review by a maintainer (owner, member or collaborator), or a maintainer labeling, assigning, closing or merging. Bots and the author do not count. An item nobody answered counts the days it has waited so far.
- **Merge time and merge rate** of the outside PRs.
- **The same for first-time contributors**: PRs whose author had nothing merged in the repository when they opened them. A newcomer is treated like them, and regular outside contributors are often answered faster. GitHub gives the author's association as it is *now*, so the author of a merged first PR appears as a CONTRIBUTOR. For those, one more request checks for commits by the author from before the PR. Other PRs by a CONTRIBUTOR count as a regular's.

## How a repository is judged

- `inactive`: archived, no push for 60 days (`--inactive-days`), or no maintainer answered any item.
- `unknown`: fewer than 5 outside items (`--min-sample`), so busy that over 500 issues and PRs were opened in the last week (the sample would cost too many calls), or the repository could not be read.
- `unhealthy`: median first answer over 5 days (`--max-response-days`), median merge over 21 days (`--max-merge-days`), or under 20% of at least 5 closed outside PRs merged (`--min-merge-rate`). A project that closes outside PRs quickly without merging them answers fast but does not take contributions. Also unhealthy when first-time contributors, with at least 4 PRs, wait over `--max-response-days` for a first answer, or have under 20% of their closed PRs merged (`--min-newcomer-merge-rate`).
- `healthy`: everything else.

## Contributing guide

Files named CONTRIBUTING or AI_POLICY in the root, `.github` and `docs` are read. Lines that mention AI together with how contributions are made (generated, assisted, tools, policy, disclose...), and lines that mention a CLA, are quoted, not judged: "we do not have a CLA" mentions one too. Many projects now close AI-written pull requests, so read these before starting.

## Limits

- About 35 API calls per repository, plus one per merged PR by a CONTRIBUTOR. The token allows 5000 an hour, so check tens of repositories, not hundreds. When the limit is reached, the remaining repositories are returned as `unknown`.
- Some organizations hide membership, so a maintainer may appear as a contributor and their answers are missed. The repository then looks slower than it is.
- PRs merged outside GitHub (for example by a script that pushes the commit) count as closed without merging.
