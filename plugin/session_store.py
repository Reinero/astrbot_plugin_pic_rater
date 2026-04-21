from typing import Any, Dict

from astrbot.api.event import AstrMessageEvent


class SessionStore:
    def __init__(self):
        self._last_sent: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def session_key(event: AstrMessageEvent) -> str:
        sid = getattr(event, "unified_msg_origin", None)
        if sid:
            return sid
        mt = getattr(event, "message_type", "")
        if mt == "group" and hasattr(event, "group_id"):
            return f"onebot:group:{getattr(event, 'group_id')}"
        if mt == "private" and hasattr(event, "user_id"):
            return f"onebot:private:{getattr(event, 'user_id')}"
        return "unknown"

    def remember(self, event: AstrMessageEvent, image_id: str | None, relpath: str | None):
        self._last_sent[self.session_key(event)] = {"id": image_id, "relpath": relpath}

    def get_last(self, event: AstrMessageEvent) -> Dict[str, Any] | None:
        return self._last_sent.get(self.session_key(event))
