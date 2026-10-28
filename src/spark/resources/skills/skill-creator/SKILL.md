---
name: skill-creator
description: Build a new Spark skill interactively. Use when the user wants to create, design, author, or improve a reusable skill, or asks what makes a good skill.
---

# Skill Creator

You are helping the user author a new Spark skill. A skill is a folder with a
SKILL.md (YAML frontmatter: name, description; then markdown instructions) and
optional bundled resources. Follow this process, conversationally, one step at
a time.

## Process

1. **Purpose.** Ask what task the skill should handle and when it should
   trigger. One question at a time.
2. **Name.** Propose a kebab-case name (letters, digits, hyphens; max 64
   characters). Confirm it with the user.
3. **Description.** Draft a single sentence that starts with what the skill
   does and includes the trigger words a user would naturally say. This line
   is shown to the model in every conversation, so make every word count.
4. **Instructions.** Draft the SKILL.md body. Keep it lean: numbered steps,
   concrete commands, expected outcomes. Anything bulky (long references,
   examples, data) belongs in a resource file, loaded on demand with
   `read_skill_resource`.
5. **Resources.** Propose reference files where useful, as
   `references/<topic>.md`. Scripts go in `scripts/` and are run by the model
   with `run_command`, so mention in the instructions exactly how to invoke
   them.
6. **Review.** Show the user the complete draft (frontmatter, body, resource
   list) and ask for corrections.
7. **Create.** Call `create_skill` with the agreed name, description,
   instructions, and resources. The user will be asked to approve the write.
   To revise an existing user skill, call `update_skill` instead.

## Quality bar

- The description alone must be enough for a model to know WHEN to load the
  skill.
- The instructions alone must be enough for a model to do the task without
  asking the user how.
- Prefer one skill that does one job well over one skill that does three jobs
  vaguely.
