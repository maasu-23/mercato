import json

from langchain_core.messages import AIMessage, HumanMessage

from agent.agent import chat


def _deserialize_history(history: list) -> list:
    messages = []
    for entry in history:
        role = entry.get("role")
        content = entry.get("content", "")
        if role == "human":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    return messages


def _serialize_history(messages: list) -> list:
    return [{"role": m.type, "content": str(m.content)} for m in messages]


def handler(event, context):
    try:
        raw_body = event.get("body", {})
        body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body

        message = body.get("message")
        history = body.get("history", [])
        user_id = body.get("user_id")

        if not message or not user_id:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Both 'message' and 'user_id' are required."}),
            }

        deserialized_history = _deserialize_history(history)
        reply, updated_messages = chat(message, deserialized_history, user_id)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {"reply": reply, "history": _serialize_history(updated_messages)}
            ),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }
