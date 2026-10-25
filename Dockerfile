FROM python:3.12-slim
RUN pip install --no-cache-dir archimate-mcp
EXPOSE 8000
