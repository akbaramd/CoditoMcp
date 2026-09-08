# Anchored patch format v1

Codito accepts a deliberately small UTF-8 patch language. It is not GNU patch and
never applies fuzzy offsets. Documents are LF-normalized for parsing, but retained
file newline style is an explicit engine decision.

```text
document      = begin, LF, 1*section, end, [LF]
begin         = "*** Begin Patch"
end           = "*** End Patch"
section       = add / update / delete
add           = "*** Add File: ", path, LF, 1*add-line
update        = "*** Update File: ", path, LF, [move-to, LF], 1*hunk
move-to       = "*** Move to: ", path
delete        = "*** Delete File: ", path, LF
hunk          = "@@", [" ", anchor], LF, 1*hunk-line
add-line      = "+", *UTF8, LF
hunk-line     = (" " / "-" / "+"), *UTF8, LF
```

An Update with `*** Move to:` atomically represents a move or rename, optionally
with content changes. A move destination has a `null` non-existence precondition.

## Matching

1. Verify every named path is canonical and declared in `base_hashes`.
2. Open and hash all existing sources from validated handles.
3. Apply hunks in document order to an in-memory snapshot. Context and removed
   lines must match exactly at one location after the prior hunk.
4. Zero matches is `context_not_found`; multiple matches is `context_ambiguous`.
5. No whitespace folding, newline guessing, offset search heuristic, or partial
   success is allowed.
6. Preflight resulting sizes, encoding policy, hardlinks, reparse state,
   destinations, write permissions, local approval, and root identity.

## Atomicity and recovery

The engine stages sibling temporary files on the same volume and fsyncs content and
the recovery journal before replacement. The journal records old/new identities and
hashes without storing secrets in relay logs. Commit order and recovery markers
make the entire batch converge to the old or new manifest after a crash; an
unresolved mixed manifest disables writes and requires local recovery.

Delete and overwrite operations never follow a reparse point and reject multiply
linked files in v1. Temporary files use unpredictable names, restrictive ACLs, and
delete-on-failure cleanup. Dry run stops before staging/replacement.

## Limits

- Document: 2 MiB UTF-8.
- Sections/touched paths: 256.
- Every touched source/destination needs a precondition.
- Binary patches, permission/ACL edits, alternate streams, and case-only renames are
  outside v1.
