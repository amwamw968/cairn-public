# 应用层：只放我们的代码，**不装任何包、不联网**。
# 依赖由 cairn-deps:local 提供；代码通过 PYTHONPATH 生效，所以改代码只需重建这一层（几秒）。
FROM cairn-deps:local

ENV CAIRN_DB_URL=sqlite:////data/cairn.db \
    PYTHONPATH=/srv/cairn/src

WORKDIR /srv/cairn

COPY pyproject.toml ./
COPY src ./src

EXPOSE 18100 18130

# 默认起 MCP（供同机 Rui 调用）；网页服务在 compose 里覆盖 command
CMD ["python", "-m", "cairn.mcp_server", "--transport", "http", "--host", "127.0.0.1", "--port", "18130"]
