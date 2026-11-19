"""An empty reply (no text, no tool call) is asked again of the same model, `empty_retries` more times (default 3),
before the next fallback model is tried or the request fails with empty_response."""
import json

from tests.test_chat_completions import _post, _stream, client, providers  # noqa: F401 - fixtures


def _replies(provider, *replies: str) -> None:
    """The provider answers with these in turn, then keeps repeating the last one."""
    queue = list(replies)

    def next_reply() -> str:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    complete, stream = provider.complete, provider.stream

    async def complete_once(request):
        provider.reply = next_reply()
        return await complete(request)

    def stream_once(request):
        provider.reply = next_reply()
        return stream(request)

    provider.complete, provider.stream = complete_once, stream_once


def test_empty_reply_is_retried_until_the_model_answers(client, providers):  # noqa: F811
    _replies(providers["llama"], "", " ", "Hello there")
    res = _post(client, model="llama")
    assert res.status_code == 200, res.text
    assert res.json()["choices"][0]["message"]["content"] == "Hello there"
    assert len(providers["llama"].requests) == 3 and res.json()["fallback_used"] is False


def test_default_is_three_retries_then_empty_response(client, providers):  # noqa: F811
    _replies(providers["llama"], "")
    res = _post(client, model="llama")
    assert res.json()["error"]["code"] == "empty_response"
    assert "(4 tries)" in res.json()["error"]["message"]
    assert len(providers["llama"].requests) == 4


def test_zero_retries_fails_on_the_first_empty_reply(client, providers):  # noqa: F811
    _replies(providers["llama"], "")
    res = _post(client, model="llama", empty_retries=0)
    assert res.json()["error"]["code"] == "empty_response" and len(providers["llama"].requests) == 1


def test_retries_run_before_the_fallback_model(client, providers):  # noqa: F811
    _replies(providers["claude"], "")
    res = _post(client, model="claude", empty_retries=1)
    assert res.status_code == 200 and res.json()["fallback_used"] is True
    assert len(providers["claude"].requests) == 2 and len(providers["gpt"].requests) == 1


def test_retry_count_is_bounded(client):  # noqa: F811
    assert _post(client, model="llama", empty_retries=11).status_code in (400, 422)
    assert _post(client, model="llama", empty_retries=-1).status_code in (400, 422)


def _content(lines: list[str]) -> str:
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if "choices" in c)


def test_streaming_retries_an_empty_stream_before_sending_anything(client, providers):  # noqa: F811
    _replies(providers["llama"], "", "Hi from llama")
    status, lines = _stream(client, model="llama")
    assert status == 200 and lines[-1] == "data: [DONE]"
    assert _content(lines).strip() == "Hi from llama"
    assert len(providers["llama"].requests) == 2


def test_streaming_reports_empty_response_after_the_retries(client, providers):  # noqa: F811
    _replies(providers["llama"], "")
    status, lines = _stream(client, model="llama", empty_retries=2)
    error = json.loads(lines[-2][6:])["error"]
    assert error["code"] == "empty_response" and "(3 tries)" in error["message"]
    assert len(providers["llama"].requests) == 3
