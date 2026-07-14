# Throwaway test script — not part of the shipped CLI. Safe to delete.
import traceback

from dotenv import load_dotenv

load_dotenv()

from agent.agent import chat

if __name__ == "__main__":
    print("Testing Mercato agent...")
    try:
        reply, messages = chat("Find me a phone under 20000 rupees", [], "test-user-123")
        print("REPLY:")
        print(reply)
        print(f"\nMessages in history: {len(messages)}")
    except Exception:
        print("chat() failed with an exception:")
        traceback.print_exc()
