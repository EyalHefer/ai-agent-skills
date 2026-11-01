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

## Development

```
pip install -r requirements-dev.txt
pytest
```
