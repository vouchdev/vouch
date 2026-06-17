"""Post-approval extraction passes that file proposals, never artifacts.

Extractors run after `proposals.approve` lands a durable artifact and only
ever call back into `proposals.propose_*`. They never call `store.put_*`
directly -- the review gate applies to extracted edges exactly like it
applies to anything an agent proposes by hand.
"""
