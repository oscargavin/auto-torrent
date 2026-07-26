"""Conversational layer over the jobs pipeline.

A thread owns an ordered list of messages; a download job is one message in it.
The jobs package stays exactly as it was — this sits above it, so the whole
progress/cancel/resume machinery is reused rather than reimplemented.
"""
