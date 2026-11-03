# AI Agent Skills

Reusable, tested building blocks for AI agents. A skill does one job and can be used by any agent.

## Skill format

```
skills/<skill-name>/
  SKILL.md      what the skill does, when to use it, how to run it
  schema.json   JSON Schema of its input and output
  scripts/      the executable code
  tests/        tests, using invented data only
```

Skills are run as command-line programs that write JSON to stdout, so an agent in any language can use them.

## Skills

| Skill | Purpose |
|---|---|
| [whatsapp-export-reader](skills/whatsapp-export-reader/SKILL.md) | Turns WhatsApp group exports into structured job postings |
| [rule-filter](skills/rule-filter/SKILL.md) | Removes items from a JSON list using keyword rules |
| [profile-matcher](skills/profile-matcher/SKILL.md) | Scores items against a profile with an LLM and ranks them |
| [job-page-reader](skills/job-page-reader/SKILL.md) | Reads job links in a headless browser and sorts them into open and closed |
| [github-repo-finder](skills/github-repo-finder/SKILL.md) | Finds popular, active GitHub repositories with good first issues |
| [repo-health-checker](skills/repo-health-checker/SKILL.md) | Measures how fast GitHub maintainers answer and merge outside contributions |
| [github-issue-lister](skills/github-issue-lister/SKILL.md) | Lists open, unassigned newcomer issues from GitHub repositories |
| [issue-reader](skills/issue-reader/SKILL.md) | Reads GitHub issue threads and sorts them into available, taken and closed |

## Development

```
pip install -r requirements-dev.txt
pytest
```
