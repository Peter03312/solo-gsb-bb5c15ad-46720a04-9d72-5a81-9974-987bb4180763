FROM python:3.12-slim

WORKDIR /app

# 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY app ./app

# SQLite 数据目录（容器内默认 /data/app.db，可用 DB_PATH 覆盖）
ENV DB_PATH=/data/app.db
RUN mkdir -p /data

EXPOSE 8000

# 启动即自动完成数据库初始化（建表 + 首次写入种子目录 v1）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
