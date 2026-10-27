# AutoPhoneAgent

跨平台、多租户的 **AI 通话助手**:用你自己的克隆音色接听与外呼电话,通过公网 Web 管理系统下达任务并查看结果。

> 本仓库只含代码与架构说明,不含任何密钥、服务器地址或部署凭据。

## 架构

三面分层:

```
控制面 (公网 VPS)    账号鉴权 · 多租户数据 · 任务队列 · WSS 中转 · 渠道配置
   ▲ HTTPS(浏览器)   ▲ WSS(边缘端从 NAT 后主动连出,家里不开端口)
执行面 (边缘端 Mac/Linux/Win + 手机)   音频 · 手机控制(WDA/ADB) · 对话编排
语音面 (GPU)         多租户 ASR / 克隆音色 TTS   +   LLM(OpenAI 兼容接口)
```

边缘端六个可替换接口:`AudioIO` / `ASRBackend` / `TTSBackend` / `PhoneAdapter` /
`Messenger` / `ServiceManager`(各平台换适配器,核心逻辑不变)。

## 目录

```
server/     控制面:FastAPI + Postgres + Redis(Docker Compose)
  api/      REST + WebSocket(多租户、JWT + TOTP 2FA、设备令牌、任务下发)
edge/       边缘端:接口定义与各平台适配器
web/        React 管理后台(开发中)
```

## 技术栈

- 后端 FastAPI · SQLAlchemy(async) · Postgres 16 · Redis
- 鉴权 JWT + TOTP 二次验证 · 设备令牌(边缘端 WSS 鉴权)
- 实时 WebSocket(边缘端与浏览器)
- 前端 React
- LLM OpenAI 兼容接口

## 本地起控制面

```bash
cd server
cp .env.example .env      # 填入随机密钥
docker compose up -d --build
# API 在 127.0.0.1:18080,反代到公网见部署文档
```

## 合规

- 通话录音遵守各地双方同意法规,提供"可能被录音/AI 协助"告知。
- 克隆音色需本人同意声明,防止冒用。
- 外呼遵守 do-not-call,被直接问及身份时如实回答。

## License

见 [LICENSE](LICENSE)。
