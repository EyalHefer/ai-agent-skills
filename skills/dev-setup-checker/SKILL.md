---
name: dev-setup-checker
description: Judge how hard it is to build and test GitHub repositories locally, from their files, their setup instructions and how long their CI takes, and sort them into easy, moderate and hard with the reasons. Use before picking an issue to work on, so a small issue does not cost an evening of installing toolchains. Requires a GitHub token.
---

# Dev Setup Checker

A small issue in a project that takes three hours to build is not a small issue. This skill reads what a contributor would meet before writing a line: the toolchains, the build system, the setup instructions and the wait for CI.

## Run

```
python scripts/dev_setup_checker.py owner/name [owner/name ...]
python scripts/dev_setup_checker.py < health.json
```

Input is repository names as arguments, or from stdin a JSON array of names or of objects with a `full_name` field (`--repo-field`), or the object written by `github-repo-finder` (`repos`), `repo-health-checker` (`healthy`) or `rule-filter` (`kept`). Each entry is returned unchanged, with a `setup` field added, under `easy`, `moderate`, `hard` or `unknown`. Output is defined in [schema.json](schema.json). Progress goes to stderr.

The token is taken from `GITHUB_TOKEN`, or from `gh auth token`.

## What is read

- **The file list**, in one request. From it:
  - the **toolchains** to install: one per kind of build file (pyproject.toml, package.json, pom.xml, Cargo.toml, go.mod, CMakeLists.txt...) in the root or one folder down, outside examples, tests, docs and vendored code
  - the **build tools** named by root files: a Gradle or Maven wrapper, uv, Poetry, npm, CMake, Bazel, Make...
  - a **devcontainer** or Gitpod setup, Docker and Nix files, and CUDA code
- **The setup instructions**: up to 3 documents, most specific first (DEVELOPMENT, HACKING, BUILDING, then CONTRIBUTING, INSTALL, README), in the root, `.github` and `docs`. From the first one with a heading about setup, building or running tests, the text right under those headings is kept (`setup_excerpt`, for a person or a model) and its command lines are counted. Subsections count only when their own heading is about setup too, since many guides put everything, optional database and documentation tooling included, under one "Development environment" heading.
- **CI time**: the latest 100 successful workflow runs triggered by pull requests. A contributor waits for the slowest workflow, so each commit counts its longest run, and the median over commits is kept.

## How a repository is judged

Each difficulty adds points:

| | points |
|---|---|
| two toolchains / three or more | 1 / 2 |
| built with Bazel or Autotools | 2 |
| CUDA code | 1 |
| no setup instructions in the repository, or more than 15 commands (`--max-setup-commands`) | 1 |
| CI over 30 minutes per PR commit (`--max-ci-minutes`) / over twice that | 1 / 2 |
| over 1 GB to clone (`--max-size-mb`) | 1 |
| a devcontainer or Gitpod setup | -1 |
| a Gradle or Maven wrapper | -1 |

0 or less is `easy`, 1-2 `moderate`, 3 or more `hard`. `reasons` lists what counted. `unknown` means the repository could not be read.

## Limits

- 3 API calls per repository, and up to 3 file downloads that do not count against the API limit.
- Setup instructions kept outside the repository, on a documentation site, are not followed. The reason then names the document that may link to them.
- Commands are counted from code blocks, and comment and output lines are mostly skipped. Treat the count as a size, not an exact number.
- CI time covers GitHub Actions only. Projects that test on other services (Buildkite, Jenkins, Prow) look faster than they are.
- For very large repositories GitHub lists only part of the files (`tree_truncated`), so a toolchain may be missed.
