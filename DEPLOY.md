# 部署说明（让评委用网址直接体验全功能）

> 核心原则：**key 作为平台密钥（环境变量）配置，绝不写进仓库**。评委访问网址即用全部功能，看不到也拿不到 key。
> 不填 key 也能跑——产品零 key 可用（内置示例库 + 离线复演），真实高德/LLM 只是增强。

---

## 一、应用怎么读 key（已支持环境变量，优先级：环境变量 > 配置文件）

| 环境变量 | 含义 |
|---|---|
| `AGENT_AMAP_KEY` | 高德 Web 服务 key（真实 POI/定位/路线图） |
| `AGENT_LLM_ENABLED` | `true` 启用真实 LLM 意图解析；不填走本地 Mock |
| `AGENT_LLM_BASE_URL` | OpenAI 兼容中转站，如 `https://你的中转站/v1` |
| `AGENT_LLM_API_KEY` | LLM key |
| `AGENT_LLM_MODEL` | 如 `claude-3-5-sonnet-20241022` |
| `PORT` | 云平台自动注入，无需手填 |

> 本地仍可用 `agent/llm_config.json`（已 gitignore，不入库）。部署只用上面的环境变量。

---

## 二、无需信用卡的部署（Render/Koyeb/HF 现都可能要卡，优先用这个）

### 方案 0：Back4app Containers（实测注册不要卡，GitHub + Docker，跑全功能）⭐
1. https://www.back4app.com → 注册（GitHub 登录，无需信用卡）。
2. 选 **Containers / Container as a Service** → **Deploy from GitHub** → 选仓库 `chenzihan0426-oss/meituan`、分支 `main`。
3. 自动识别 `Dockerfile`；**Container Port 填 `7860`**（容器监听此口）。
4. **Environment Variables** 填 key（平台密钥，不进仓库）：
   `AGENT_AMAP_KEY`、`AGENT_LLM_ENABLED=true`、`AGENT_LLM_BASE_URL`、`AGENT_LLM_API_KEY`、`AGENT_LLM_MODEL`。
5. Deploy → 拿到网址发评委。常驻容器 → 内存会话正常、真实高德/LLM 可用。

> 备注：Vercel/Netlify 虽不要卡，但它们是 **Serverless**，本产品的多步会话存在服务器内存里，
> 在无服务器环境会频繁"会话过期"，不适合；要用就得把会话改存外部数据库（不划算）。

### 方案 A：Koyeb（连 GitHub 自动部署；部分地区注册需验证/卡）
1. https://www.koyeb.com → 用 **GitHub 登录**。
2. **Create Web Service** → **GitHub** → 选仓库 `chenzihan0426-oss/meituan`、分支 `main`。
3. Builder 选 **Dockerfile**（仓库已带 `Dockerfile`）；端口 Koyeb 会自动用 `$PORT`，无需改。
4. **Environment variables** 填 key（平台密钥，不进仓库）：
   `AGENT_AMAP_KEY`、`AGENT_LLM_ENABLED=true`、`AGENT_LLM_BASE_URL`、`AGENT_LLM_API_KEY`、`AGENT_LLM_MODEL`。
5. Deploy → 拿到 `https://xxx.koyeb.app` 网址发评委。
   > 免费档 1 个服务、无访问时 scale-to-zero（首次访问稍慢），不用卡、不过期。

### 方案 B：Hugging Face Spaces（免费、无需信用卡、2 核 16G）
1. https://huggingface.co 注册 → **New Space**。
2. **Space SDK 选 Docker**（空白模板）；可见性 Public。
3. 把本仓库文件传上去（网页上传，或 `git push` 到 Space 的仓库）——确保含 `Dockerfile` 与 `agent/`。
   > Space 自带的 `README.md` 顶部要有 `sdk: docker`（建 Space 时自动生成的那份，保留它）。
4. **Settings → Variables and secrets → New secret** 填上面那几个 key。
5. 等容器构建完，Space 页面就是可用网址（监听 7860，HF 自动路由）。
   > 免费档约 48 小时无访问休眠，有人访问会自动唤醒。

> 两者都"无需信用卡"。Koyeb 操作最接近 Render；HF 资源更足但要把代码放到 HF 仓库。

---

## 三、Render 部署（需绑卡，备选）

1. 代码推到 GitHub（已就绪：含 `Procfile`、`requirements.txt`、`render.yaml`）。
2. 打开 https://render.com → New → **Web Service** → 连接本仓库。
3. 配置（若没自动读取 `render.yaml`）：
   - **Runtime**: Python 3
   - **Build Command**: 留空
   - **Start Command**: `python -m agent.web`
4. **Environment → 添加密钥**（就是上表那几个，逐个填值；这是平台密钥，不进仓库）：
   `AGENT_AMAP_KEY`、`AGENT_LLM_ENABLED=true`、`AGENT_LLM_BASE_URL`、`AGENT_LLM_API_KEY`、`AGENT_LLM_MODEL`
5. Deploy → 拿到一个 `https://decisionmate-xxxx.onrender.com` 网址，发给评委即可。

> 其它平台同理：Railway / Fly.io / 一台云服务器（VPS）都行——启动命令 `python -m agent.web`，把上面几个 key 设为环境变量。

---

## 四、本地快速验证"部署形态"（用环境变量、不用配置文件）

```bash
AGENT_AMAP_KEY=你的高德key \
AGENT_LLM_ENABLED=true AGENT_LLM_BASE_URL=https://你的中转站/v1 \
AGENT_LLM_API_KEY=你的LLMkey AGENT_LLM_MODEL=claude-3-5-sonnet-20241022 \
PORT=8000 python -m agent.web
# 浏览器开 http://127.0.0.1:8000 —— 此时 key 全部来自环境变量
```

---

## 五、给评委的话术（无需任何 key）

> "在线版直接点网址体验全部功能；想本地跑的话，`python3 -m agent.web` 即可，**不填任何 key 也能完整演示**（内置示例库），真实高德/LLM 为可选增强。"

---

## ⚠️ 关于"把 key 公开到 GitHub"

**不要这么做。** LLM key=真金白银，公开几分钟即被爬虫盗刷、并被服务商自动吊销（评委反而用不了）；高德 key 会被刷爆配额。一旦推上去进了 git 历史就收不回。
**正确做法就是本文档：key 放部署平台的密钥栏，评委用网址即享全功能、永远接触不到 key。**
