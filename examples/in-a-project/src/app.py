# Part of the real application. Repo code like this is never read or uploaded by
# agentlift — it isn't under .managed-agents/.

def handler(event):
    return {"ok": True, "echo": event}
