import hashlib
import json
import traceback

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


def _user_id_from_request_context(event: dict) -> str | None:
    """Derive user_id from the SigV4-verified caller identity, never the request body.

    API Gateway HTTP APIs populate requestContext.authorizer.iam.userArn with the
    ARN of the IAM principal that signed the request once the route's
    AuthorizationType is AWS_IAM. Hashing it the same way cli/main.py does means a
    given AWS identity gets the same user_id whether it comes in through the CLI
    or through this endpoint.
    """
    arn = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("iam", {})
        .get("userArn")
    )
    if not arn:
        return None
    return hashlib.sha256(arn.encode("utf-8")).hexdigest()[:16]


def handler(event, context):
    try:
        user_id = _user_id_from_request_context(event)
        if not user_id:
            return {
                "statusCode": 401,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Missing or unverified caller identity."}),
            }

        raw_body = event.get("body", {})
        body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body

        message = body.get("message")
        history = body.get("history", [])

        if not message:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "'message' is required."}),
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
    except Exception:
        # Full detail goes to CloudWatch; the caller only ever sees a generic message
        # so stack traces, table names, and other internals never leak in the response.
        print(traceback.format_exc())
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Internal server error"}),
        }
