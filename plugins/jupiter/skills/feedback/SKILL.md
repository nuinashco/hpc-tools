---
name: feedback
description: Prepares a report on this session's use of the jupiter plugin for the plugin's maintainers and files it as an issue once the user has read it and agreed. For when the plugin gave wrong advice about JUPITER, a `jup` command misbehaved, or the cost check asked wrongly or missed something.
argument-hint: "[what went wrong]"
disable-model-invocation: true
---

# Sending feedback on the jupiter plugin

The user asked for this by name. A report leaves their machine only on their explicit
yes, after they have seen what is in it; your part is to prepare it, show it honestly
and send it only then.

## 1. Prepare the report

Pass what the user wrote as the note, through a quoted here-document so that no
character of theirs is read by the shell. If they wrote nothing, ask in one question
what went wrong and what they expected, and use their answer.

```bash
jup feedback - <<'JUP_NOTE'
$ARGUMENTS
JUP_NOTE
```

This writes a file and sends nothing. It reads this session's transcript and keeps
only the turns in which the plugin was used: the user's requests in those turns, the
`jup`, `ssh` and Slurm commands with their output, the plugin's skills that were
loaded and what Claude answered. It adds the helper's local log of runs and
cost-check decisions. Tokens, keys, e-mail and network addresses, the home path, and
the login and project names are replaced by placeholders, and the output says how
many of each. A session too long for one report keeps its latest turns; `--whole`
includes every turn, also those without the plugin.

## 2. Show it

Tell the user, from the command's output: where the file is, how many turns it holds,
what was taken out, and where it would go (the repository named in the last line).
Then read the file and give them a short, plain account of what a maintainer would
see in it, naming anything that looks private and was not caught: the replacement is
by pattern and misses what it has no pattern for. Offer to print the whole file.

Ask plainly whether to send it. Do not send on anything short of a clear yes, and do
not take silence, an earlier request or this skill's invocation as one.

## 3. Send or discard

```bash
jup feedback --send FILE        # files it as an issue; prints the issue's address
jup feedback --discard FILE     # deletes it; nothing was sent
```

If they want something removed first, edit the file as they say, show the change, and
ask again. After sending, give them the issue's address. If `gh` is not logged in, the
command says so: they run `gh auth login`, or attach the file to an issue themselves.
