# Personal AI

> 我的统一 AI 入口与个人 AI 数据层。
> History、Memory、Task、项目上下文**属于我本人**，不属于任何一家模型；
> Model / Agent / Provider **三层都可以替换**。

永久时间骨架：**过去 · 现在 · 未来**。

---

## 当前状态（如实说明，不夸大）

| 能力 | 状态 |
|---|---|
| 后端服务（零第三方依赖） | ✅ 已实现 |
| Provider 可替换（注册表机制） | ✅ 已实现，已接入 `mock` + `openai_compat` |
| History 持久化（SQLite） | ✅ 已实现，单文件可拷走 |
| 来源透明（回答携带真实调用链） | ✅ 已实现 |
| 过去/现在/未来 时间骨架 UI | ✅ 已接入真实接口（A3） |
| **未来区（Task）** | ✅ 已完成（A4）：任务落库、推进状态、删除，三段全部由真实数据驱动 |
| 自动化测试 | ✅ 21/21 通过（`python scripts/smoke_test.py`） |
| **真实模型调用** | ✅ **已验证 2026-10-06** —— 经本机 AI 网关 → SiliconFlow → `deepseek-ai/DeepSeek-V3.2`，实测见下方「可核验的证据」 |
| **流式输出（SSE，让回答像 ChatGPT 一样边生成边显示）** | ✅ 已完成（A5）：真实模型实测 16～33 个增量块，首块 1.6s（热） |
| Memory / Task（未来区） | ⛔ 未实现，仅有视觉占位 |
| 多端同步 | ⛔ 未实现 |

`mock` Provider 是**显式标注**的离线兜底：回答以 `[mock]` 开头，调用链显示 `mock → mock-offline`，界面上会提示"未产生真实模型调用"。不伪装。

---

## 快速开始

零第三方依赖。装了 Python 3.9+ 就能跑，不需要 pip install 任何东西。

```bash
python -m personal_ai --serve          # 离线模式（mock Provider，不调用真实模型）
python scripts/start_local.py          # 一键启动并自动接入本机 OmniRoute 网关
```

打开 <http://127.0.0.1:8787>

`start_local.py` 会自动检查网关是否在运行、取出本机已有的网关 Key（只读，不新建、不改配置），
Key 只存在于进程内，不写入任何文件。

```bash
python -m personal_ai --status                 # 查看当前 provider 与配置
python -m personal_ai --once "你好"             # 单次问答并打印真实调用链
python scripts/smoke_test.py                   # 冒烟测试（21 项）
```

### 接一个真实模型

密钥只从环境变量读，**不落盘、不进 git**：

```bash
export PAI_PROVIDER=openai_compat
export PAI_BASE_URL=https://api.siliconflow.cn/v1
export PAI_API_KEY=你的Key
export PAI_MODEL=deepseek-ai/DeepSeek-V3.2

python -m personal_ai --serve
```

凡是暴露 `/chat/completions` 的供应商都能直接用：SiliconFlow、DeepSeek 官方、OpenAI、Moonshot、通义、vLLM、Ollama，以及 OmniRoute 这类本地网关。

---

## 架构

```mermaid
graph TB
    subgraph UI["入口层 · 过去 / 现在 / 未来"]
        WEB["personal_ai/web/<br/>时间轴 UI（原生 HTML/CSS/JS）"]
    end

    subgraph SERVER["服务层"]
        HTTP["server/app.py<br/>零依赖 HTTP（stdlib http.server）"]
        CORE["core/chat.py<br/>编排：落库 → 调模型 → 落库"]
    end

    subgraph DATA["数据层 · 属于用户，不属于模型"]
        DB[("storage/sqlite.py<br/>sessions + messages<br/>单文件 .db，可拷走")]
    end

    subgraph EXEC["可替换执行层"]
        REG["providers/ 注册表"]
        P1["mock<br/>离线兜底"]
        P2["openai_compat<br/>OpenAI 兼容协议"]
    end

    subgraph EXT["外部"]
        M1["SiliconFlow / DeepSeek /<br/>OpenAI / vLLM / Ollama / OmniRoute"]
    end

    WEB -->|"fetch /api/*"| HTTP
    HTTP --> CORE
    CORE -->|"读写"| DB
    CORE --> REG
    REG --> P1
    REG --> P2
    P2 --> M1

    style DATA fill:#1a3a2a,stroke:#5fd39a,color:#e8f5ee
    style EXEC fill:#2a2440,stroke:#a9b6ff,color:#eceaff
    style EXT fill:#2c2c2a,stroke:#888780,color:#e6e6ef
```

**一句话**：入口只有一层薄壳，数据落在你自己的文件里，模型只是插槽里的零件。

---

## 目录结构

```
personal_ai/
├── config.py                 配置；密钥只从环境变量读
├── providers/
│   ├── base.py               Provider 抽象 + 调用链 chain
│   ├── mock.py               离线兜底，显式标注
│   ├── openai_compat.py      OpenAI 兼容协议
│   └── __init__.py           注册表（新增供应商 = 新增一个子类）
├── storage/sqlite.py         sessions + messages
├── core/chat.py              编排层
├── server/app.py             HTTP 服务 + 静态资源
├── web/                      时间轴前端（index.html / app.js / style.css）
└── __main__.py               CLI
scripts/
├── smoke_test.py             冒烟测试（自动拉起服务并跑 32 项检查）
└── start_local.py             一键启动 + 自动接入本机 AI 网关
```

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 + 当前 provider / 调用链 / 配置（不含密钥） |
| GET | `/api/providers` | 所有可替换 provider 及其配置状态 |
| GET | `/api/sessions` | 会话列表（**过去**） |
| POST | `/api/sessions` | 新建会话 |
| GET | `/api/sessions/<id>/messages` | 会话历史（**过去**） |
| POST | `/api/chat` | 发消息（**现在**），返回 `content` + `chain` + `is_mock` |
| POST | `/api/chat/stream` | **流式**发消息（SSE），逐块推送 `data: {delta}`，以 `[DONE]` 收尾 |
| GET | `/api/tasks` | 任务列表（**未来**） |
| POST | `/api/tasks` | 新建任务（**未来**） |
| PATCH | `/api/tasks/<id>` | 改任务状态 / 标题（**未来**） |
| DELETE | `/api/tasks/<id>` | 删除任务（**未来**） |

每条回答都带 `chain`，例如：

```
openai-compat → api.siliconflow.cn → deepseek-ai/DeepSeek-V3.2
```

而不是只有一个模型名。这是「来源透明」原则的落地。

---

## 设计原则

1. 过去 / 现在 / 未来是主体结构，不是三个普通功能模块。
2. 新能力从侧面进入，不抢占时间轴。
3. 数据属于用户：换机器 = 拷走一个 `.db` 文件。
4. 一切可替换：Model / Agent / Provider 都不与业务耦合。
5. 状态必须真实：不伪装 Loading，不伪装模型名。
6. 一次只改变一个变量。
7. 免费优先，但不假设任何第三方额度永久存在——用「可替换」保证未来。

---

## 如何新增一家供应商

```python
from personal_ai.providers import ProviderBase, register

@register
class MyProvider(ProviderBase):
    name = "my_provider"
    display_name = "我的供应商"

    def complete(self, messages):
        ...  # 返回 ChatResponse(content=..., provider=..., model=..., chain=[...])

    def default_chain(self):
        return ["my_provider"]
```

然后 `PAI_PROVIDER=my_provider`。上层代码零改动。

---

## Roadmap

- [x] A1 后端骨架 + Provider 适配层
- [x] A2 History 持久化（SQLite）
- [x] A3 时间轴 Web 入口 + 真实会话
- [x] A4 简单 Task / Memory（**未来**区真正工作）
- [x] A5 流式输出（SSE）
- [ ] A6 历史导入（ChatGPT / DeepSeek 导出文件）
- [ ] A7 多端访问

项目内部的任务树、决策记录与职业资料保存在本机 `_agent_center/`（含个人信息，**不随本仓库公开**）。

---

## 已知未实现项

- Memory（「未来」区的记忆能力）、历史导入、多端同步均未实现。
- 历史导入、多端同步未实现。
- 真实模型调用**已于 2026-10-06 验证通过**，不再是未验证项。

---

## 可核验的证据

这个项目里所有"已完成"都可以自己复查，不需要相信作者。

### 1. 自动化测试：32 项

```bash
python scripts/smoke_test.py
```

覆盖：健康检查 · 供应商可替换性 · 会话创建 · 对话 · 历史落库 · 会话列表 ·
错误处理（400 / 404）· 任务增删改查 · 前端三件套 · **流式输出 6 项**

### 2. 真实模型调用（2026-10-06）

| 项 | 实测值 |
|---|---|
| 调用链 | `openai-compat → 本机网关 → deepseek-ai/DeepSeek-V3.2` |
| 网关侧记录 | 4 次 HTTP 200，含 token 统计（第三方独立佐证） |
| 多轮上下文 | 第 1 轮"记住数字 7314"，第 2 轮正确答出 → 上下文确实进了模型 |

**怎么自己复现**（需要你本机有一个 OpenAI 兼容端点）：

```bash
export PAI_PROVIDER=openai_compat
export PAI_BASE_URL=<你的端点地址>
export PAI_API_KEY=<你的 key>
export PAI_MODEL=<模型名>
python -m personal_ai --once "你好"
```

期望输出里出现 `是否离线兜底: 否`。

### 3. 流式输出实测

| 场景 | 首块到达 | 完成 | 增量块数 |
|---|---|---|---|
| 上游冷启动 | 11.71s | 14.26s | 33 |
| 上游已热 | **1.63s** | 2.45s | 16 |

### 4. 上游冷启动的稳定性改造

发现上游首字可能耗时 50～115 秒（网关日志实测），因此：

- 请求超时从 **60 秒 → 180 秒**
- 加入**自动重试 2 次**（仅对超时 / 5xx / 限流；4xx 不重试）
- 压测：关闭网关 → 重启 → 连发 5 次 → **5/5 成功**（51.8s / 2.3 / 2.6 / 1.4 / 1.7）

### 5. 一个踩过的坑（也写在这里）

用 Python 标准库写 SSE（Server-Sent Events，服务端事件流）时，
`BaseHTTPRequestHandler` 默认 HTTP/1.0，若响应头声明 `Connection: keep-alive`，
客户端会一直等结束标记而挂起。修复：流式响应显式 `self.close_connection = True`。

---

## 关于本仓库的范围

本仓库只发布产品代码与说明。**项目内部的任务树、决策记录、工作流水账与职业资料保存在本机 `_agent_center/`，
含个人信息，不随本仓库公开。** 这样做是刻意的：代码可以公开，私人资料不公开。

---

## 手机 / 平板访问

默认只允许本机访问。要让手机连同一个 WiFi 也能打开：

```bash
python -m personal_ai --serve --host 0.0.0.0
```

启动后会打印两个地址：本机地址和局域网地址（例如 `http://192.168.1.5:8787`）。

> ⚠️ **安全提醒**：切到 `0.0.0.0` 后，**同一个局域网内的任何设备**都能打开这个页面，
> 也能读到你的本地对话历史（SQLite 单文件）。请只在可信网络下使用，
> 用完改回默认值（不写 `--host` 即可）。

Windows 首次运行可能会弹出防火墙提示，选择"允许"才能从手机访问。
