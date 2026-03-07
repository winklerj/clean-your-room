# How to manage GitHub repositories

## Add a repository

1. Navigate to `/repos/new`.
2. Paste the GitHub URL in the input field. Use the `https://github.com/org/repo` format.
3. Submit the form.

The application parses the URL, extracts the org and repo name, and clones the repository to:

```
~/.clean-room/repos/org--repo/
```

## View a repository

Navigate to `/repos/{id}` to see the repo detail page. This page displays:

- The list of jobs previously run against the repo
- A prompt selector for launching new spec-generation jobs

## Archive a repository

Send a POST request to archive a repo you no longer need:

```
POST /repos/{id}/archive
```

From the repo detail page, click **Archive** to trigger this action. Archived repos no longer appear in the default repo list.
