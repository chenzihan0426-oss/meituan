# DecisionMate · 通用容器（Koyeb / Hugging Face Spaces / 任意 Docker 主机）
# 纯 Python 标准库，无需 pip 安装。
FROM python:3.11-slim
WORKDIR /app
COPY . .
# HF Spaces 默认路由到 7860；Koyeb 等会在运行时注入 $PORT 覆盖它。web.py 读 $PORT。
ENV PORT=7860
EXPOSE 7860
CMD ["python", "-m", "agent.web"]
