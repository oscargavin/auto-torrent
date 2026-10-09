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
