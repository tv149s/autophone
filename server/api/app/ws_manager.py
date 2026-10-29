"""边缘端 WSS 连接登记表。进程内维护 device_id -> WebSocket 的映射,
把任务下推给对应边缘端,并接收其状态/结果上报。
(单实例够用;将来多实例再用 Redis pub/sub 扩展。)"""
import asyncio
import json

from fastapi import WebSocket


class Hub:
    def __init__(self) -> None:
        self._conns: dict[str, WebSocket] = {}
        self._outbox: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def connect(self, device_id: str, ws: WebSocket) -> None:
        async with self._lock:
            old = self._conns.get(device_id)
            self._conns[device_id] = ws
            self._outbox[device_id] = asyncio.Queue()   # 新连接配新队列,旧队列丢弃
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
                self._outbox.pop(device_id, None)
                return True
        return False

    def is_online(self, device_id: str) -> bool:
        return device_id in self._conns

    def outbox(self, device_id: str) -> "asyncio.Queue | None":
        return self._outbox.get(device_id)

    async def send(self, device_id: str, message: dict) -> bool:
        """把待推送消息放进该设备的发送队列,由其 WS 协程统一发送。
        不再跨协程直接对 WebSocket 写(新版 starlette/uvicorn 下跨协程 send 不可靠)。
        default=str 让 datetime 等对象也能安全序列化。"""
        q = self._outbox.get(device_id)
        if q is None:
            return False
        await q.put(json.loads(json.dumps(message, default=str)))
        return True


hub = Hub()

# 人工接入会话的运行状态:device_id -> status(边缘端上报,网页轮询读)。
# 进程内即可,和 hub 同生命周期(单实例够用)。
manual_status: dict[str, dict] = {}
