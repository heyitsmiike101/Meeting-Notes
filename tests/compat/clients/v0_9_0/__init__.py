"""Frozen network layer of the Meeting Notes 0.9.0 recorder (the wire protocol and endpoints are
identical to 0.8.9's; queue.py's local stale-claim handling, control_channel.py's slower retry after a 4409
"replaced" close, and the notes co-editing command set_notes in remote.py / control_channel.py differ)."""

__version__ = "0.9.0"
COMMIT = "main/0.9.0"
