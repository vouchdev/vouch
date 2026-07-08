---
name: vouch-record
description: Propose a typed record (contact, org, project) into the vouch KB
---

# /vouch-record

File "$ARGUMENTS" as a typed record: an entity plus a kind-validated page,
both as pending proposals. Requires the company-brain page kinds
(`vouch init --template company-brain`; check with `vouch schema list`).

Steps:

1. Decide the record shape: person -> entity type `person` + `contact` page
   (frontmatter: `role`, optional `org`, `email`); organisation -> `company`
   entity + `org` page; project -> `project` entity + `project-record` page
   (frontmatter: `record_status`, optional `owner`).
2. Call `kb_search` first — if the entity already exists, propose only the
   page update (pass the existing page id as `slug_hint`) instead of a
   duplicate.
3. Call `kb_propose_entity`, then `kb_propose_page` with `page_type` set to
   the record kind and the frontmatter in `metadata`. Cite a registered
   source when the record distills one (register the conversation via
   `kb_register_source` if the user is dictating facts).
4. Link ownership or membership with `kb_propose_relation` where it's clear.
5. Report every proposal id filed.

Never call `kb_approve`. One record per invocation; ask before batching.
