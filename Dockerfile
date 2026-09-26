FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# requirements-runtime.txt 用 `-r` 引用了 requirements-base.txt，
# 两个文件必须一起 COPY：漏了 base 会让 pip 在构建期直接失败
# （这个错误很晚才暴露——前面的 pip install 依赖层可能已经被缓存了）。
COPY requirements-runtime.txt requirements-base.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY . .

EXPOSE 8000

CMD ["/bin/sh", "/app/entrypoint.sh"]