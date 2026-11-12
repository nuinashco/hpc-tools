---
name: feedback
description: Prepares a report on this session's use of the jupiter plugin for the plugin's maintainers, has a digest made of it, and files the digest as an issue once the user has read it and agreed. For when the plugin gave wrong advice about JUPITER, a `jup` command misbehaved, or the cost check asked wrongly or missed something.
argument-hint: "[what went wrong]"
disable-model-invocation: true
---

# Sending feedback on the jupiter plugin

The user asked for this by name. Nothing leaves their machine without their explicit
yes, given after they have read what would go. What goes is a short digest, because
an issue may be readable by anyone; the full report stays with them. Your part is to
prepare both, show the digest honestly, and send it only then.

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
includes every turn, also those without the plugin. The output also says who can read
an issue on the repository the report would go to.

## 2. Have the digest made

Start the plugin's `feedback-digest` agent (subagent type `jupiter:feedback-digest`)
with one sentence: `Write the digest of the report at FILE.` It is a small model that
sees the report and nothing of this session, which is the point: it cannot carry
anything over from the rest of the conversation. Its answer is the digest. Hand it on
as it came, without adding to it from what you know:

```bash
jup feedback --digest FILE <<'JUP_DIGEST'
<the agent's answer>
JUP_DIGEST
```

This takes names and secrets out once more, writes the digest beside the report and
prints its path. If no agent can be started in this session, write the digest
yourself by the rules in `${CLAUDE_PLUGIN_ROOT}/agents/feedback-digest.md`, from the
report alone.

## 3. Show it

Print the whole digest for the user: it is short, and it is exactly what would be
filed. Tell them, from the commands' output, where it would go and who can read it
there, and that the full report stays on their machine at its path. Name anything in
the digest that still looks private: the replacement is by pattern, and the agent can
miss things too.

Ask plainly whether to send it. Do not send on anything short of a clear yes, and do
not take silence, an earlier request or this skill's invocation as one.

## 4. Send or discard

```bash
jup feedback --send DIGEST      # files the digest as an issue; prints its address
jup feedback --discard FILE     # deletes the report and its digest; nothing was sent
```

If they want something changed first, edit the digest as they say, show the change,
and ask again. If they would rather send the full report, `--send FILE` takes it too:
say once more who can read it there. After sending, give them the issue's address. If
`gh` is not logged in, the command says so: they run `gh auth login`, or attach the
file to an issue themselves.
