"""Smart reading: a small language model that runs on this PC and finds the folder names in
an email from a sender who is in no department.

Nothing here decides anything alone. The model only *proposes* names; each one is looked up on
the storage server, every request from such a sender waits for the person's approval, and what
the model says is never used as a path, a command or an address.
"""
