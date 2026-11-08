"""What is specific to IETF mailing lists, appended to the generic system prompt for ``ietf_mail`` sources."""

IETF_FRAGMENT = """\
IETF CONTEXT
- The messages come from public IETF working group mailing lists. Documents are Internet-Drafts named draft-<group-or-author>-<topic>-<NN>; the trailing number is the revision. Keep draft names exactly as written.
- Consensus is "rough consensus", declared by the working group chairs (or the responsible Area Director for documents outside a working group). A chair or Area Director stating that consensus exists, or that a call ended with a decision, is the strongest evidence for "agreed". Several participants saying "+1", "I support" or "no objection" make "leaning" unless a chair or the participants themselves state the matter is settled.
- Typical process messages: a call for adoption (should the working group take this document on), a working group last call (WGLC: final review before the chair sends the document on), a hum or poll at a meeting, a shepherd write-up, an IETF Last Call, an AD review. Report which of these is in progress only if the messages or events say so, including any stated deadline.
- Authors of a draft often answer reviewers: distinguish "the author agrees to change the text" (agreed in principle, text not yet published) from "the new revision contains the change".
- Notices from the Datatracker or the I-D submission tool arrive as events, not as discussion. Do not treat them as someone's position.
- Meeting references (for example "IETF NNN") and deadlines belong in "next" when stated.
"""
