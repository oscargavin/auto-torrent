"""A stand-in for claude_agent_sdk.ClaudeSDKClient.

`script(prompt, options)` is an async generator of SDK messages, the same shape
the old `query()` stubs had, so each turn replays it against the new prompt.
"""


def fake_client(script):
    class FakeClient:
        instances: list = []

        def __init__(self, options):
            self.options = options
            self.prompts: list[str] = []
            self.connected = False
            FakeClient.instances.append(self)

        async def connect(self):
            self.connected = True

        async def query(self, prompt):
            self.prompts.append(prompt)
            self._messages = script(prompt, self.options)

        async def receive_response(self):
            async for msg in self._messages:
                yield msg

        async def disconnect(self):
            self.connected = False

    return FakeClient


async def _no_ask(*_a):
    pass


async def one_turn(text, thread_id, settings, sink, session=None, **kw):
    """One turn on a session of its own (closed after), or on `session`."""
    from auto_torrent.server.agent import AgentSession

    own = session is None
    session = session or AgentSession()
    kw.setdefault("on_ask", _no_ask)
    try:
        return await session.turn(text, settings, sink, **kw)
    finally:
        if own:
            await session.close()


def bound_tools(sink, *, on_ask=None, settings=None, raw_query="dune", **turn):
    """A session's tool handlers, bound to one turn's sink and callbacks, for
    calling the tools directly."""
    from auto_torrent.server.agent import AgentSession

    session = AgentSession()
    session._new_turn(raw_query, settings, sink, on_ask or _no_ask, **turn)
    return session, {t.name: t.handler for t in session._tools}
