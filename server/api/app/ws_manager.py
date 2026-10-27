"""边缘端 WSS 连接登记表。进程内维护 device_id -> WebSocket 的映射,
把任务下推给对应边缘端,并接收其状态/结果上报。
(单实例够用;将来多实例再用 Redis pub/sub 扩展。)"""
import asyncio
import json

from fastapi import WebSocket


class Hub:
    def __init__(self) -> None:
        self._conns: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, device_id: str, ws: WebSocket) -> None:
        async with self._lock:
            old = self._conns.get(device_id)
            self._conns[device_id] = ws
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass

    async def disconnect(self, device_id: str, ws: WebSocket) -> bool:
        """移除连接。返回 True 表示移除的确实是当前连接(未被新连接替换),
        调用方据此决定是否把设备标记为离线,避免重连竞态把在线覆盖成离线。"""
        async with self._lock:
            if self._conns.get(device_id) is ws:
                self._conns.pop(device_id, None)
                return True
        return False

    def is_online(self, device_id: str) -> bool:
        return device_id in self._conns

    async def send(self, device_id: str, message: dict) -> bool:
        ws = self._conns.get(device_id)
        if ws is None:
            return False
        try:
            # default=str 让 datetime 等对象也能安全序列化
            await ws.send_text(json.dumps(message, default=str))
            return True
        except Exception:
            return False


hub = Hub()
