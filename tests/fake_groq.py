"""In-memory stand-in for Groq's chat-completions endpoint (tests only)."""
import json

import httpx


def facts(service=None, budget=None, start=None, role=None, company=None, intent=None) -> dict:
    """Build an extraction answer. Each arg is (value, quote); budget is (amount, currency, quote)."""
    def f(pair):
        return {"value": pair[0], "quote": pair[1]} if pair else {"value": None, "quote": None}

    return {
        "service": f(service), "start_within_months": f(start), "role": f(role),
        "company": f(company), "intent": f(intent),
        "budget": ({"amount": budget[0], "currency": budget[1], "quote": budget[2]} if budget
                   else {"amount": None, "currency": None, "quote": None}),
    }


class FakeGroq:
    def __init__(self):
        self.down = True                 # default: unavailable, so unrelated tests never reach a model
        self.extract = facts()           # dict, raw string, or callable(body) -> dict | str
        self.writer = ""                 # str or callable(body) -> str
        self.calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        if self.down:
            return httpx.Response(503, json={"error": {"message": "service unavailable"}})
        if "response_format" in body:
            answer = self.extract(body) if callable(self.extract) else self.extract
            content = answer if isinstance(answer, str) else json.dumps(answer)
            usage = {"prompt_tokens": 900, "completion_tokens": 120}
        else:
            content = self.writer(body) if callable(self.writer) else self.writer
            usage = {"prompt_tokens": 250, "completion_tokens": 60}
        return httpx.Response(200, json={"model": body["model"], "usage": usage,
                                         "choices": [{"message": {"role": "assistant", "content": content}}]})

    @property
    def extraction_calls(self) -> int:
        return sum(1 for c in self.calls if "response_format" in c)
