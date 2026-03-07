# How to manage prompts for spec generation

## View all prompts

Navigate to `/prompts` in your browser. The page lists every prompt by name with its template text.

## Create a new prompt

1. On the `/prompts` page, click **Add Prompt**.
2. Enter a **name** for the prompt (e.g., "Generate Security Spec").
3. Write the **template text** in the text area.
4. Click **Save**.

### Template content suggestions

Prompt templates can include instructions covering any combination of the following:

- **Provable Properties Catalog** -- which properties the agent should identify and catalog
- **Purity Boundary Map** -- where pure and impure boundaries exist in the codebase
- **Verification Tooling Selection** -- which verification tools the agent should recommend
- **Property Specifications** -- formal property specs the agent should produce
- **Mermaid diagrams** -- request architectural or flow diagrams in Mermaid syntax

## Edit an existing prompt

1. On the `/prompts` page, click the prompt you want to modify.
2. Edit the name or template text inline (changes are submitted via HTMX partial updates).
3. Click **Save** to persist changes.

## Delete a prompt

1. On the `/prompts` page, locate the prompt to remove.
2. Click **Delete** next to the prompt entry.
3. Confirm the deletion when prompted.

## Default prompts

The application ships with two built-in prompts:

| Prompt name      | Purpose                                          |
|------------------|--------------------------------------------------|
| Create Spec      | Generates a new specification from the repository |
| Improve Spec     | Refines and improves an existing specification    |

These defaults can be edited or deleted like any user-created prompt.
